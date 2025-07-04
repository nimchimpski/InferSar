import torch
from pathlib import Path
import shutil
import rasterio
import numpy as np
from tqdm import tqdm
import time
import os
import xarray as xr
import json
import matplotlib.pyplot as plt
import click
import yaml
import gc
import logging
from rasterio.plot import show
from rasterio.windows import Window
from rasterio.warp import calculate_default_transform, reproject, Resampling
from scripts.train.train_classes import UnetModel
from scripts.train.train_helpers import pick_device
from scripts.process.process_tiffs import  create_event_datacube_TSX_inf,reproject_to_4326_gdal, make_float32_inf, resample_tiff_gdal
from scripts.process.process_dataarrays import tile_datacube_rxr_inf
from scripts.process.process_helpers import  print_tiff_info_TSX, check_single_input_filetype, rasterize_kml_rasterio, compute_image_minmax, process_raster_minmax, path_not_exists, read_minmax_from_json, normalize_imagedata_inf, read_raster, write_raster
from collections import OrderedDict
from skimage.morphology import binary_erosion

start=time.time()

logging.basicConfig(
    level=logging.INFO,                            # DEBUG, INFO,[ WARNING,] ERROR, CRITICAL
    format=" %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S")

logger = logging.getLogger(__name__)

device = pick_device()

def create_weight_matrix(tile_size, overlap_size):
    """Generate a weight matrix using cosine decay for blending."""
    weight = np.ones((tile_size, tile_size), dtype=np.float32)

    # Cosine weights for overlap regions
    edge_weight = 0.5 * (1 + np.cos(np.linspace(-np.pi, 0, overlap_size)))
    weight[:overlap_size, :] *= edge_weight[:, None]  # Top edge
    weight[-overlap_size:, :] *= edge_weight[::-1][:, None]  # Bottom edge
    weight[:, :overlap_size] *= edge_weight[None, :]  # Left edge
    weight[:, -overlap_size:] *= edge_weight[::-1][None, :]  # Right edge

    return weight


def make_prediction_tiles(tile_folder, metadata, model, device, threshold, ):
    print(f'---ORIGINAL PREDICTIONS FUNCTION')
    predictions_folder = Path(tile_folder).parent / f'{tile_folder.stem}_predictions'
    if predictions_folder.exists():
        print(f"--- Deleting existing predictions folder: {predictions_folder}")
        # delete the folder and create a new one
        shutil.rmtree(predictions_folder)
    predictions_folder.mkdir(exist_ok=True)

    for tile_info in tqdm(metadata, desc="Making predictions"):
        tile_path = tile_folder /  tile_info["tile_name"]
        pred_path = predictions_folder / tile_info["tile_name"]

        with rasterio.open(tile_path) as src:
            tile = src.read(1).astype(np.float32)  # Read the first band
            profile = src.profile   
            nodata_mask = src.read_masks(1) == 0  # True where no-data

        # Prepare tile for model
        tile_tensor = torch.tensor(tile).unsqueeze(0).unsqueeze(0).to(device)  # Add batch and channel dims

        # Perform inference
        with torch.no_grad():
            pred = model(tile_tensor)
            # pred = torch.sigmoid(pred).squeeze().cpu().numpy()  # Convert logits to probabilities
            pred = torch.sigmoid(pred).squeeze().cpu().numpy()  # Convert logits to probabilities
            pred = (pred > threshold).astype(np.float32)  # Convert probabilities to binary mask
            pred[nodata_mask] = 0  # Mask out no-data areas

        # Save prediction as GeoTIFF
        profile.update(dtype=rasterio.float32)
        with rasterio.open(pred_path, "w", **profile) as dst:
            dst.write(pred.astype(np.float32), 1)

    return predictions_folder

def make_prediction_tiles_new(tile_folder, metadata, model, device, threshold, stride):
    predictions_folder = Path(tile_folder).parent / f'{tile_folder.stem}_predictions'
    if predictions_folder.exists():
        print(f"--- Deleting existing predictions folder: {predictions_folder}")
        shutil.rmtree(predictions_folder)
    predictions_folder.mkdir(exist_ok=True)

    # DETERMINE THE OVERALL OUTPUT SHAPE
    tile_size = 256
    stride = tile_size
    overlap = tile_size - stride

    # CREATE A WEIGHT MATRIX FOR BLENDING
    weight_matrix = create_weight_matrix(tile_size, overlap)

    # DETERMINE THE OVERALL OUTPUT DIMENSIONS
    max_x = max([tile_info['x_start'] for tile_info in metadata]) + tile_size
    max_y = max([tile_info['y_start'] for tile_info in metadata]) + tile_size
    global_shape = (max_x, max_y)

    # INITIALIZE ARRAYS FOR MERGING PREDICTIONS
    global_prediction = np.zeros(global_shape, dtype=np.float32)
    global_weight_sum = np.zeros(global_shape, dtype=np.float32)

    #.................
    # GET A TILE FORM THE METADATA
    for tile_info in tqdm(metadata, desc="Making predictions"):
        tile_path = tile_folder / tile_info["tile_name"]
        x, y = tile_info['x_start'], tile_info['y_start']

        # OPEN THE TILE
        with rasterio.open(tile_path) as src:
            tile = src.read(1).astype(np.float32)  # Read the first band
            profile = src.profile   
            nodata_mask = src.read_masks(1) == 0  # True where no-data

        # PREPARE TILE FOR MODEL
        tile_tensor = torch.tensor(tile).unsqueeze(0).unsqueeze(0).to(device)  # Add batch and channel dims

        # PERFORM INFERENCE
        with torch.no_grad():
            pred = model(tile_tensor)
            # pred = torch.sigmoid(pred).squeeze().cpu().numpy()  # Convert logits to probabilities
            pred = torch.sigmoid(pred).squeeze().cpu().numpy()  # Convert logits to probabilities
            pred[nodata_mask] = 0  # Mask out no-data areas

        # ADD WEIGHTED PREDICTION TO GLOBAL ARRAYS
        global_prediction[x:x+tile_size, y:y+tile_size] += pred * weight_matrix
        global_weight_sum[x:x+tile_size, y:y+tile_size] += weight_matrix

    # NORMALIZE GLOBAL PREDICTIONS BY WEIGHT SUM
    global_weight_sum[global_weight_sum == 0] = 1  # Prevent division by zero
    final_prediction = global_prediction / global_weight_sum
    final_prediction = (final_prediction > threshold).astype(np.float32)
      # Convert probabilities to binary mask


    # SAVE FINAL MERGED PREDICTION AS GEOTIFF
    profile.update(dtype=rasterio.float32, height=global_shape[0], width=global_shape[1])
    merged_path = predictions_folder / "merged_prediction.tif"
    with rasterio.open(merged_path, "w", **profile) as dst:
        dst.write(final_prediction.astype(np.float32), 1)

    return predictions_folder


def stitch_tiles(metadata, prediction_tiles, save_path, image):
    ''''
    metadata =list
    '''
    # GET CRS AND TRANSFORM
    with rasterio.open(image) as src:
        transform = src.transform
        crs = src.crs
        height, width = src.shape
        print('>>>src shape:',src.shape)
    
        # INITIALIZE THE STITCHED IMAGE AND COUNT
        # give stitched_image the same crs, transform and shape as the source image
        stitched_image = np.zeros((height, width))
        # print(">>>stitched_image dtype:", stitched_image.dtype)
        print(">>>stitched_image shape:", stitched_image.shape)
        #print unique values in the stitched image
        # print(f'>>>unique values in empty stitched image: {np.unique(stitched_image)}')

    for tile_info in tqdm(metadata, desc="Stitching tiles"):
        tile_name = tile_info["tile_name"]
        # Extract position info from metadata
        x_start, x_end = tile_info["x_start"], tile_info["x_end"]
        y_start, y_end = tile_info["y_start"], tile_info["y_end"]

        # Find the corresponding prediction tile
        tile = prediction_tiles / tile_name

        # Load the tile
        with rasterio.open(tile) as src:
            tile = src.read(1).astype(np.float32)
            # Debugging: Print tile info and shapes
            # print(f">>>Tile shape: {tile.shape}")
        # print(f">>> Tile info: {tile_info}")

        # Extract the relevant slice from the stitched image
        stitched_slice = stitched_image[y_start:y_end, x_start:x_end]
        if (stitched_slice.shape[0] == 0) or (stitched_slice.shape[0] == 1):
            continue
        
        # Validate dimensions
        if stitched_slice.shape != tile.shape:
            if (stitched_slice.shape[0] == 0) or (stitched_slice.shape[1] == 0):
                continue
            print(f"---Mismatch: Stitched slice shape: {stitched_slice.shape}, ---Tile shape: {tile.shape}")
            slice_height, slice_width = stitched_slice.shape
            tile = tile[:slice_height, :slice_width]  # Crop tile to match slice
            # Debugging: Print the new tile shape
            print(f">>>New tile shape: {tile.shape}")


        # Add the tile to the corresponding position in the stitched image
        stitched_image[y_start:y_end, x_start:x_end] += tile
        # PRINT STITCHED IMAGE SIZE
        # print(f">>>Stitched image shape: {stitched_image.shape}")
    print(f'---crs: {crs}')
    # Save the stitched image as tif, as save_path
    with rasterio.open(
        save_path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype=stitched_image.dtype,
        crs=crs,
        transform=transform,
    ) as dst:
        dst.write(stitched_image, 1)
    # with rasterio.open(save_path) as src:
    #     print("No-data value:", src.nodata)
        
    return stitched_image


def clean_checkpoint_keys(state_dict):
    """Fix the keys in the checkpoint by removing extra prefixes."""
    cleaned_state_dict = OrderedDict()
    for key, value in state_dict.items():
        if key.startswith("model.model."):
            new_key = key.replace("model.model.", "model.")
        elif key.startswith("model."):
            new_key = key.replace("model.", "")
        else:
            new_key = key
        cleaned_state_dict[new_key] = value
    return cleaned_state_dict

@click.command()
@click.option('--test', is_flag=True, help='loading from test folder', show_default=False)

def main(test=False):

    # import matplotlib.pyplot as plt
    # wm = create_weight_matrix(256, 64)
    # plt.imshow(wm, cmap="viridis")
    # plt.colorbar()
    # plt.show()
    print(f'>>>test mode = {test}')


    # VARIABLES................................................................
    norm_func = 'logclipmm_g' # 'mm' or 'logclipmm'
    stats = None
    MAKE_TIFS = True
    MAKE_DATAARRAY= True
    # stride = tile_size
    ############################################################################
    # DEFINE PATHS
    # DEFINE THE WORKING FOLDER FOR I/O
    predict_input = Path("/Users/alexwebb/laptop_coding/floodai/UNOSAT_FloodAI_v2/data/4final/predict_input")
    print(f'>>>working folder: {predict_input}')
    if path_not_exists(predict_input):
        return
    
    minmax_path = Path("/Users/alexwebb/laptop_coding/floodai/UNOSAT_FloodAI_v2/configs/global_minmax_INPUT/global_minmax.json")
    if path_not_exists(minmax_path):
        return

    ckpt_path = Path("/Users/alexwebb/laptop_coding/floodai/UNOSAT_FloodAI_v2/checkpoints/ckpt_INPUT")

    ############################################################################
    if test:
        threshold =  0.8 # PREDICTION CONFIDENCE THRESHOLD
        tile_size = 512 # TILE SIZE FOR INFERENCE
        # Normalize all paths in the config
        image = check_single_input_filetype(predict_input, 'image', '.tif')
        if image is None:
            print(f"---No input image found in {predict_input}")
            return
        else:
            print(f'>>>found input image: {image.name}')
        output_folder = predict_input
        output_filename = '_x'
        # analysis_extent = Path('Users/alexwebb/floodai/UNOSAT_FloodAI_v2/data/4final/predict_INPUT/extent_INPUT')  

    # READ CONFIG
    else:
        config_path = Path('/Users/alexwebb/laptop_coding/floodai/UNOSAT_FloodAI_v2/configs/floodaiv2_config.yaml')
        with open(config_path, "r") as file:
            config = yaml.safe_load(file)
        threshold = config["threshold"] # PREDICTION CONFIDENCE THRESHOLD
        tile_size = config["tile_size"] # TILE SIZE FOR INFERENCE
        # Normalize all paths in the config
        input_file = Path(config['input_file'])
        output_folder = Path(config['output_folder'])
        output_filename = Path(config['output_filename'])
        # analysis_extent = Path(config['analysis_extent'])

    stride = tile_size # STRIDE FOR TILING, SAME AS TILE SIZE

    # print(f'>>> config = {config}')
    print(f'>>>image: {image}')
    print(f'>>>threshold: {threshold}') 
    print(f'>>>tile_size: {tile_size}')
    print(f'>>>output_folder= {output_folder}')
    print(f'>>>output_filename= {output_filename}')
    # print(f'>>>alalysis_extent= {analysis_extent}')
    # print(f'>>> IF TRAINING: CHECK LAYERDICT NAMES=FILENAMES IN FOLDER <<<')
    # FIND THE CKPT
    ckpt = next(ckpt_path.rglob("*.ckpt"), None)
    if ckpt is None:
        print(f"---No checkpoint found in {ckpt_path}")
        return
    print(f'>>>ckpt: {ckpt.name}')

    # poly = check_single_input_filetype(predict_input,  'poly', '.kml')
    # if poly is None:
        # return

    # GET REGION CODE FROM MASK TODO
    # sensor = image.parents[1].name.split('_')[:1]
    sensor = 'sensor'
    # print(f'>>>datatype= ',sensor[0])
    # date = image.parents[1].name.split('_')[0]
    date = 'date'
    # print(f'>>>date= ',date)
    # image_code = "_".join(image.parents[3].name.split('_')[4:])
    # image_code = "_".join(image.parents[1].name.split('_')[1])
    parts = image.name.split('_')
    image_code = "_".join(parts[:-1])
    # print(f'>>>image_code= ',image_code)
    save_path = output_folder / f'{sensor}_{image_code}_{date}_{tile_size}_{threshold}{output_filename}WATER_AI.tif'

    print(f'>>>save_path: {save_path.name}')
    if save_path.exists():
        print(f"---overwriting existing file! : {save_path}")
        # try:
        #     print(f"--- Deleting existing prediction file: {save_path}")
        #     save_path.unlink()
        # except Exception as e:
        #     print(f"--- Error deleting existing prediction file: {e}")
        #     return

    # CREATE THE EXTRACTED FOLDER
    extracted = predict_input / f'{image_code}_extracted'

    
    print(f'>>> MAKE_TIFS = {MAKE_TIFS}')

    if MAKE_TIFS:
        if extracted.exists():
            # print(f"--- Deleting existing extracted folder: {extracted}")
            # delete the folder and create a new one
            shutil.rmtree(extracted)
        extracted.mkdir(exist_ok=True)

        # CHANGE DATATYPE TO FLOAT32
        print('>>>CHANGING DATATYPE')
        image_32 = extracted / f'{image_code}_32.tif'
        make_float32_inf(image, image_32)
        # print_tiff_info_TSX(image_32, 1)

        # RESAMPLE TO 2.5
        # print('>>>RESAMPLING')
        # resamp_image = extracted / f'{image_32.stem}_resamp'
        # resample_tiff_gdal(image_32, resamp_image, target_res=2.5)
        # print_tiff_info_TSX(resamp_image, 2)

        # with rasterio.open(image) as src:
            # print(f'>>>src shape= ',src.shape)

        # SORT OUT ANALYSIS EXTENT

        # ex_extent = extracted / f'{image_code}_extent.tif'
        # create_extent_from_mask(image, ex_extent)
        # rasterize_kml_rasterio( poly, ex_extent, pixel_size=0.0001, burn_value=1)

        # REPROJECT IMAGE
        print('>>>REPROJECTING')
        final_image = extracted / 'final_image.tif'
        reproject_to_4326_gdal(image_32, final_image, resampleAlg = 'bilinear')
        # print_tiff_info_TSX(final_image, 3)

        # reproj_extent = extracted / f'{image_code}_4326_extent.tif'
        # reproject_to_4326_gdal(ex_extent, reproj_extent)
        # fnal_extent = extracted / f'{image_code}_32_final_extent.tif'
        # make_float32_inf(reproj_extent, final_extent

    final_image = extracted / 'final_image.tif'

    # GET THE TRAINING MIN MAX STATS
    statsdict =  read_minmax_from_json(minmax_path)
    stats = (statsdict["min"], statsdict["max"])

    if MAKE_DATAARRAY:
        create_event_datacube_TSX_inf(predict_input, image_code)

    cube = next(predict_input.rglob("*.nc"), None)  
    save_tiles_path = predict_input /  f'{image_code}_tiles'

    if save_tiles_path.exists():
        # print(f">>> Deleting existing tiles folder: {save_tiles_path}")
        # delete the folder and create a new one
        shutil.rmtree(save_tiles_path)
        save_tiles_path.mkdir(exist_ok=True, parents=True)
        # CALCULATE THE STATISTICS

    # DO THE TILING
    tiles, metadata = tile_datacube_rxr_inf(cube, save_tiles_path, tile_size=tile_size, stride=stride, norm_func=norm_func, stats=stats, percent_non_flood=0, inference=True) 
    # print(f">>>{len(tiles)} tiles saved to {save_tiles_path}")
    # print(f">>>{len(metadata)} metadata saved to {save_tiles_path}")
    # metadata = Path(save_tiles_path) / 'tile_metadata.json'

    # INITIALIZE THE MODEL
    # device=torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    device=pick_device()
    model = UnetModel( encoder_name="resnet34", in_channels=1, classes=1, pretrained=False 
    )   
    model.to(device)
    # LOAD THE CHECKPOINT
    ckpt_path = Path(ckpt)
    checkpoint = torch.load(ckpt_path)

    cleaned_state_dict = clean_checkpoint_keys(checkpoint["state_dict"])

    # EXTRACT THE MODEL STATE DICT
    # state_dict = checkpoint["state_dict"]

    # LOAD THE MODEL STATE DICT
    model.load_state_dict(cleaned_state_dict)

    # SET THE MODEL TO EVALUATION MODE
    model.eval()

    prediction_tiles = make_prediction_tiles(save_tiles_path, metadata, model, device, threshold)

    # STITCH PREDICTION TILES
    prediction_img = stitch_tiles(metadata, prediction_tiles, save_path, final_image)
    # print prediction_img size
    # print(f'>>>prediction_img shape:',prediction_img.shape)
    # display the prediction mask
    # plt.imshow(prediction_img, cmap='gray')
    # plt.show()

    del model

    torch.cuda.empty_cache()
    gc.collect()

    end = time.time()
    # time taken in minutes to 2 decimal places
    print(f"Time taken: {((end - start) / 60):.2f} minutes")

if __name__ == "__main__":
    main()