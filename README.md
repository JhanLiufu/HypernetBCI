# HypernetBCI
Quick calibration of DL-based BCI models enabled by hypernetworks

## Training scripts
* **MI** refers to the motor imagery paradigm, and **SS** refers to the sleep staging paradigm. If the script ends with **_torch**, that means it's using the torch training framework rather than braindecode NeuralClassifier.

* **{MI, SS}_baseline_1(_torch).py**: baseline 1 experiments are the ones that train a model **from scratch** for each person using different amount of training data. 

* **{MI, SS}_baseline_2(_torch).py**: baseline 2 experiments are the ones that take each person as the hold out / new person and pretrain the BCI model on everyone else together as a big pretrain pool. No distinction between people in the pretrain model. This is a **transfer learning** approach.

* **{MI, SS}_HN_sanity_check.py**: as the name suggests, this is a sanity check experiment. It takes arbitrary primary network **XYZ** and builds a hypernetwork over it. The entire architecture is referred to as **HyperXYZ**. **HyperXYZ** is trained with all training data from an individual subject to make sure that it at least performs similarly as the original network. There is no transfer / calibration / adaptation here.
