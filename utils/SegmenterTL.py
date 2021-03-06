"""
Training loops for detection model

"""
import os
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from scipy.ndimage import zoom

from tqdm import tqdm
import dsntnn

import torch
import torch.nn as nn
from torchsummary import summary
from torch.optim import Adam
import torch.optim.swa_utils as swa

from torchviz import make_dot
import graphviz
from torch.autograd import Variable

import seaborn as sns

import utils.customModel_v2 as cm2
import utils.customWriter_v2 as cw2
from utils.customLosses import multi_class_dice, EarlyStopping, FocalLoss


class Segmenter():
    """
    ~Class for training vertebrae detector/segmentation model
    @params: 
      dir_name = directory name used for splitting tensorboard runs.   
    """
    def __init__(self, training=None, validation=None, testing=None, dir_name=None, device="cuda:0", 
                    batch_size=4, n_outputs=13, learning_rate=3e-3, num_epochs=200, output_path='./outputs/', 
                    model_path=None, SWA=False, classifier=False):
        self.device = torch.device(device)
        torch.cuda.set_device(self.device)
        self.train_dataLoader = training
        self.val_dataLoader = validation
        self.test_dataLoader = testing
        self.classifier = classifier

        self.model = cm2.customUNet(n_outputs=n_outputs, classifier=classifier).cuda()
        self.optimizer = Adam(self.model.parameters(), lr=learning_rate)
        self.es = EarlyStopping(patience=75)
        #* Losses
        self.criterion = FocalLoss(apply_sigmoid=True).cuda()
        self.dice = multi_class_dice().to(device)
        self.bce = nn.BCEWithLogitsLoss().to(device) if classifier else None
        self.scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(self.optimizer, mode='min', verbose=True)
        
        self.writer = cw2.customWriter(log_dir=f'./runs/{dir_name}', batch_size=batch_size, num_classes=n_outputs)
        self.best_loss = 10000 # Initialise loss for saving best model
        self.num_epochs = num_epochs
        self.output_path = output_path
        self.model_path = model_path

        #** Stochastic Weight Averaging (https://pytorch.org/docs/stable/optim.html#putting-it-all-together)
        self.swa = SWA
        self.swa_model = swa.AveragedModel(self.model) if SWA else None
        self.swa_scheduler = swa.SWALR(
            self.optimizer, swa_lr=0.05) if SWA else None  # LR set to large value
        self.swa_start = 100 if SWA else None  # START EPOCH from SWA
    
    @staticmethod
    def sigmoid(x):
        return 1/(1+np.exp(-x))

    @staticmethod
    def norm_img(img):
        return (img-img.min())/(img.max()-img.min())

    def forward(self, num_epochs=200, model_name='best_model.pt'):
        #~ Forward pass def
        for epoch in range(num_epochs+1):
            self.writer.epoch = epoch
            print(f'Epoch: {epoch}/{num_epochs}')
            #~TRAINING
            self.train(epoch=epoch, write2tensorboard=True, writer_interval=20)
            #~ VALIDATION
            self.validation(epoch=epoch, write2tensorboard=True, writer_interval=1)
            #* Save best model + Check early stopping criteria
            stop = self.save_best_model(model_name=model_name)
            if stop:
                break
        
        #* Update batch norm stats. for SWA model
        if self.swa:
            print('Updating batch norm stats')
            for data in tqdm(self.train_dataLoader):
                img = data['sag_image'].to(self.device, dtype=torch.float32)
                self.swa_model(img)
            print('Saving best model')
            torch.save(self.swa_model.state_dict(),
                       self.output_path + f"{model_name.split('.')[0]}_SWA.pt")


    def train(self, epoch, write2tensorboard=True, writer_interval=20, viz_model=False):
        #~Main training loop
        #@param:
        #    writer_interval = write to tensorboard every x epochs

        #* Allow param optimisation & reset losses
        self.model.train()
        self.writer.reset_losses()
        #*Visualise model using graphviz
        if viz_model:
            self.viz_model(output_path='./logs/')
        # ** Training Loop **
        for idx, data in enumerate(tqdm(self.train_dataLoader)):
            #*Load data
            img = data['sag_image'].to(self.device, dtype=torch.float32)
            mask = data['mask'].to(self.device, dtype=torch.float32)
            self.optimizer.zero_grad() #*Reset gradients
            if self.classifier:
                labels = data['labels'].to(self.device, dtype=torch.float32)
                pred_seg, pred_labels = self.model(img)
                bce = self.bce(pred_labels, labels)
            else:
                pred_seg = self.model(img)
            #* Loss + Regularisation
            ce = self.criterion(pred_seg, mask)
            #dsc = self.dice(pred_seg, mask)
            #loss = dsc + ce
            loss=ce
            if self.classifier:
                loss += bce/100
            self.writer.train_loss.append(loss.item())
            #* Optimiser step
            loss.backward()
            self.optimizer.step()
            if write2tensorboard:
                # ** Write inputs to tensorboard
                if epoch % writer_interval ==0  and idx == 0:
                    self.writer.plot_mask(
                        f'Ground-truth', img=img, prediction=mask)
            
        print('Train Loss:', np.mean(self.writer.train_loss))
        self.writer.add_scalar('Training Loss', np.mean(
            self.writer.train_loss), epoch)
            
    def validation(self, epoch, write2tensorboard=True, writer_interval=10):
        #~Validation loop
        with torch.set_grad_enabled(False):
            print('Validation...')
            for idx, data in enumerate(tqdm(self.val_dataLoader)):
                #* Load data
                img = data['sag_image'].to(self.device, dtype=torch.float32)
                mask = data['mask'].to(self.device, dtype=torch.float32)
                pred_seg= self.model(img)
                if self.classifier:
                    labels = data['labels'].to(self.device, dtype=torch.float32)
                    pred_seg, pred_labels = self.model(img)
                    bce = self.bce(pred_labels, labels)
                else:
                    pred_seg = self.model(img)

                #* Loss 
                ce = self.criterion(pred_seg, mask)
                #dsc= self.dice(pred_seg, mask)
                #val_loss = ce+dsc
                val_loss = ce
                if self.classifier:
                    val_loss += bce/100
                    self.writer.bce.append(bce.item()/100)

                self.writer.ce.append(ce.item())
                #self.writer.dsc.append(dsc.item())
                self.writer.val_loss.append(val_loss.item())
                
                if write2tensorboard:
                    #* Write predictions to tensorboard
                    if epoch % writer_interval == 0 and idx==0:
                        #plot_mask = torch.argmax(pred_seg, dim=1, keepdim=True)
                        plot_mask = pred_seg
                        self.writer.plot_mask(
                            f'Predicted mask', img=img, prediction=plot_mask, apply_sigmoid=True)
            print('Validation Loss:', np.mean(self.writer.val_loss))
            if self.swa:
                if epoch > self.swa_start:
                    self.swa_model.update_parameters(self.model)
                    self.swa_scheduler.step()
            else:
                self.scheduler.step(np.mean(self.writer.val_loss))
            self.writer.add_scalar('Validation Loss', np.mean(self.writer.val_loss), epoch)
            self.writer.add_scalar('DSC', np.mean(self.writer.dsc), epoch)
            self.writer.add_scalar('CE', np.mean(self.writer.ce), epoch)
            if self.classifier:
                self.writer.add_scalar('BCE', np.mean(self.writer.bce), epoch)

    def inference(self, model_name='best_model.pt', plot_output=False, save_preds=False):
        #~ Model Inference
        print('Inference...')
        if self.model_path is None:
            self.model.load_state_dict(torch.load(self.output_path + model_name))
        else:
            self.model.load_state_dict(
                torch.load(self.model_path + model_name))
        self.model.eval()
        all_ids = []
        all_masks = []
        all_labels = []
        with torch.set_grad_enabled(False):
            for idx, data in enumerate(tqdm(self.test_dataLoader)):
                #* Load data
                img = data['sag_image'].to(
                    self.device, dtype=torch.float32)
                ids = data['id']
                #* Get predictions
                if self.classifier:
                    pred_seg, pred_labels = self.model(img)
                else:
                    pred_seg = self.model(img)
               
                if plot_output:
                    os.makedirs(os.path.join(self.output_path, 'sanity'), exist_ok=True)
                    #* Plot predictions
                    self.plot_mask(ids, torch.argmax(
                        pred_seg, dim=1, keepdim=True), img)
                all_ids.append(ids)
                all_masks.append(pred_seg.cpu().numpy())
                if self.classifier:
                    all_labels.append(pred_labels.cpu().numpy())

        all_ids = np.concatenate(all_ids, axis=0)
        all_masks= np.concatenate(all_masks, axis=0)
        if self.classifier:
            all_labels = np.concatenate(all_labels, axis=0)
            print(all_ids.shape, all_masks.shape, all_labels.shape)
        else:
            print(all_ids.shape, all_masks.shape)
        if save_preds:
            #** Save predictions to npz file for post-processing
            print('SAVING PREDICTIONS...')
            if self.classifier:
                np.savez(self.output_path + f'{model_name.split(".")[0]}_preds.npz', ids=all_ids,
                         masks=all_masks, labels=all_labels)
            else:
                np.savez(self.output_path + f'{model_name.split(".")[0]}_preds.npz', ids=all_ids,
                            masks=all_masks)
        else:
            if self.classifier:
                return all_ids, all_masks, all_labels
            else:
                return all_ids, all_masks

    def viz_model(self):
        #~ View model architecture
        print('Vis model')
        input_shape = (4, 3, 512, 256) 
        model = self.model
        #* Create placeholder
        x = Variable(torch.randn(input_shape)).to('cuda')
        mask = model(x)
        #* Only follow path of coordinates
        graph = make_dot(mask.mean(), params=dict(model.named_parameters()))
        graph.render(self.output_path + 'graph.png')
        #summary(model, input_shape)

    def write2file(self, array, targets, epoch,output_path='./logs/gifs/'):
        #~ Write arrays to file for sanity checking 
        #* Colormaps for heatmaps
        plt.style.use('dark_background')
        cmap = sns.cubehelix_palette(
            start=0.5, rot=-1., hue=1, gamma=1, as_cmap=True)
        arr = array.cpu().detach().numpy()
        targets = targets.cpu().numpy()
        dist = zoom(input=arr, zoom=(10, 1), order=1)
        tgt = zoom(input=targets, zoom=(10, 1), order=1)
        fig, ax = plt.subplots(1, 2, figsize=(5, 10))
        ax[0].set_title(f'Epoch: {epoch}')
        ax[1].set_title('Ground-truth')
        ax[0].imshow(dist.T, cmap=cmap)
        ax[1].imshow(tgt.T, cmap=cmap)
        fig.savefig(output_path + f'no_scheduler_heatmap_epoch_{epoch}.png')    
        plt.close() 

    def save_best_model(self, model_name='best_model.pt'):
        #~ Check if latest validation is min. If True, save.
        loss1 = np.mean(self.writer.val_loss)
        is_best = loss1 < self.best_loss
        self.best_loss = min(loss1, self.best_loss)
        if is_best:
            print('Saving best model')
            torch.save(self.model.state_dict(),
            self.output_path + model_name)
        
        #~ Early Stopping
        if self.es.step(torch.tensor([loss1])):
            return True
        else:
            return False
    
    def plot_mask(self, names, pred, img):
        #~ Plot predictions (sanity check)
        fig, ax = plt.subplots(1, 1, figsize=(10, 10))
        ax.axis('off')
        for idx in np.arange(len(names)):
            arr = pred[idx].cpu().numpy()
            arr = self.sigmoid(arr)
            img = np.moveaxis(img[idx].cpu().numpy(), 0, -1)
            img = self.norm_img(img)
            ax.imshow(img)
            ax.imshow(arr[idx], alpha=0.5)
            fig.savefig(self.output_path + f'sanity/{names[idx]}.png')
            plt.clf()
        plt.close()


