This document is still under development, and parts stilll refer to FloodAI_V2 upon which InferSAR was built - apologies!

# InferSAR

INFERSAR is a lightweight field tool developed from FloodAI_V2, itself originally developed for the United Nations Satellite Centre (UNOSAT) to support rapid, high-accuracy flood mapping during emergency response operations. It was created from scratch as a fully modular, maintainable, and scalable codebase, designed to integrate smoothly into operational workflows. Existing preprocess code structure is optimised for training on the Sen1Floods11 dataset, but it also supports additional geospatial inputs like Digital Elevation Models (DEM) and terrain slope for improved segmentation performance in complex landscapes. Built on PyTorch Lightning, the system includes GPU-accelerated training, flexible architecture support, and seamless logging via Weights & Biases.

### Preprocessing Enhancements:   
The preprocessing pipeline extracts the relevant data from Geotifs concatenating it into  xarray datasets along with other data such as DEM and SLOPE, which is then converted into machine-learning-ready tiles, with mappable layers as model input channels.
Normalization steps included   log scaling, clipping, and Min-Max normalization   for SAR data, ensuring consistency during training and inference. Normalization is shown to have a huge impact on training results.
A   parameterized tile selection process   was trialled and partially implemented, allowing controlled class balancing and landcover diversity.  Current class balance selection is random, with insufficient control to achieve a varied selection of landcover types in the tiles meeting the desired percentage below the required threshold. Use of  STAC metadata to one simple and powerful way to solve this.

### Directory Structure:   
The new pipeline is structured as a Python package  with modular scripts.  

### Logging:   
Integrated with   WandB   for detailed tracking of training metrics, including AUC-PR, F1, IoU, Precision, and Recall, as well plots and visualizations of prediction outputs compared to ground truths.

### Training Efficiency Improvements:    
 Experiments showed that training times decreased by   >11%   when datasets were moved to a local drive, reducing the time for the continual reading of the files which was creating a bottleneck.
Optimal performance was achieved with   8 workers (parallel processes)  ; increasing workers beyond this led to resource contention and slower training.  

### Model Architecture:    
 A  UNet model  pertained on ImageNet weights, with AdamW optimizer with weight decay, is used . There are drop out layers to reduce over fitting and a learning rate scheduler (to slow it down as the convergence process completes). The binarizing threshold is usually best at around 0.8.
Training required ~30 minutes for 15 epochs on 10,000–20,000 samples, with no significant improvements observed up to 400 epochs.  This training time is a significant improvement on previous measures. Partly due to smaller datasets, but also improvements in the code, libraries, and data location.

## Key Notes for Future Developers:    
Further split up functions, using a Functional Programming approach (no side effects, limited I/O writes, return value based
Add Unit tests.
Explore overlapping tiling to improve model predictions at tile edges which is usually recommended. However, only few and certain data seem to cause tiling artifacts, so that may be an upstream problem solved in preprocessing.
Completing STAC protocol integration for metadata access and fine grained dataset control.
Access the Jira project management Wiki which is up to date.

## Generalization Testing and Results  
  
The second dataset -  TerraSARX - was latterly focused on to see if the use of higher resolution data alone (average 2.5m pixels compared to Sentinal1 10m/pixel) would yield improved results. Discrepancies in the data distribution were found between sensor mission modes (strip, scan, wide and spot) effecting test and inference results. Hence a subset was made of just the highest resolution data - comprising more consistent and better data not withstanding the reduction in test data size.
To assess model generalization,   K-fold region holdout testing   was conducted. 
This contrasted to earlier and commonly used distinct test split but from the same data set.
Results showed:  
Strong generalization   across most regions, with AUC-PR values reaching   0.85   and F1 scores averaging   0.8 , and crucially, visually obvious water bodies being well defined.

Using automated parameter sweeping, I was able to iterate through multiple configurations of loss function, class balance, threshold etc. 
This led to the choice of a BCE+DICE combined loss function with BCE weighting of 0.35  - this yielded best AUC-PR. Dynamic weighting was disabled for the BCE part. Metric wise,  
I erred towards rating Recall (not missing actual flood) over Precision (not creating false alerts) as missed floodwaters are arguably more consequential in flood analysis than false alarms.
Focal loss was explored with multiple alpha and gamma value combinations, but , with the Terrasarx data, appreared to suffer from misclassification of structures and roads (possible low confidence pixels getting over compensated for which is what Focal loss excels at).
The AUC-PR is (rather easily) inflated by using a (probably unrealistically) balanced dataset. The figures look great - but when later attempting inference and testing  on a geographically distinct region  with a (to be expected)  lower class balance and different data distribution, the results drop considerably and the model chosen will start over predicting (making false alarms). If training and inference are limited to a restricted region the needs for generalization are naturally much less.
An immediately obvious caveat to the testing here is that the first instance (choice of balanced dataset) almost necessarily involves a drastically smaller data subset - so suffers from a decrease in training data size, and all that that entails.
The final class balance decided on was 0.25 (majority non flood).
Results indicate one should aim to have a class ratio that best matches that which is most probable in the inference data. The converse was shown;  a massively flooded inference SAR image (ratio 0.85 ) was one of the problem inputs, with the model showing confused predictions.

Earlier the Test (evaluation) step were done using a subset of the data used for training - a fairly common technique. Using a 0.7, 0.15, 0.15 train/validate/test split, it was found that by introducing a large amount of tiles featuring mountain terrain coupled with zero flood ground truth labels, the model could effectively be taught that that ‘mountain shadow’ was not water (a persistent problem). This needs to be tested using K-fold / hold-1-region method. 

## USING InferSAR TO ANALYZE AN IMAGE - 

### THE ENVIRONMENT
If necessary run:
Conda activate floodenv2
(For pre-processing functions use floodenv3)
Dependencies are in  environment.yml, package root level.

### THE CONFIG FILE

Takes an unescaped  backslashed windows path for the input SAR file, input analysis extent and output folder location.
Takes threshold and tilesize. (0 to 1) and (256 or 512) respectively
Takes a file name - this is appended to the already added datatype, filename, tilesize, threshold.
 
 ## PREPROCESSING

 run_process.py
 Use this to prepare raw training data:
 -Prepare geotifs
 -create datacubes for each scene/event
 -create normalized tiles from the datacubes

DATASET FOLDER
A collection of folders (events).
The event foldername should contain its unique code (find and edit '# GET REGION CODE FROM FOLDER' )

Each event folder must contain:
-image tif (*image.tif) - VV polarisation 
-mask tif (*mask.tif)

