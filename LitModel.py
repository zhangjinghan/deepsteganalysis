import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)
import torch
import torch.nn.functional as F
import pytorch_lightning as pl
from optimizers import get_optimizer, get_lr_scheduler, get_lr_scheduler_params
import models.surgeries
from models.models import get_net
from metrics import Accuracy, wAUC, PE, MD5
    
class LitModel(pl.LightningModule):
    """
    Train a steganalysis model
    """
    def __init__(self, args) -> None:
        
        self.args = args
        super().__init__()
        self.save_hyperparameters(self.args)
        
        self.train_metrics = {'train_mPE': PE()}
        self.validation_metrics = {'val_acc': Accuracy(), 'val_wAUC': wAUC(), 'val_mPE': PE(), 'val_MD5': MD5()}
        
        self.__set_attributes(self.train_metrics)
        self.__set_attributes(self.validation_metrics)
        self.__build_model()
    
    def __set_attributes(self, attributes_dict):
        for k,v in attributes_dict.items():
            setattr(self, k, v) 

    def __build_model(self):
        """Define model layers & loss."""
        # 1. Load pre-trained network:
        self.net = get_net(self.args.model.backbone, 
                           num_classes=3, #TODO automate this
                           in_chans=1, #TODO automate this
                           imagenet=self.args.ckpt.imagenet, 
                           ckpt_path=self.args.ckpt.seed_from)
        
        # 2. Do surgery if needed
        if self.args.model.surgery is not None:
            self.net = getattr(models.surgeries, self.args.model.surgery)(self.net)

        # 3. Loss:
        self.loss_func = F.cross_entropy

    def forward(self, x):
        """Forward pass. Returns logits."""

        x = self.net(x)
        
        return x

    def loss(self, logits, labels):
        return self.loss_func(logits, labels)

    def training_step(self, batch, batch_idx):
        # 1. Forward pass:
        x, y = batch
        y_logits = self.forward(x)
        
        # 2. Compute loss:
        train_loss = self.loss(y_logits, y)
            
        # 3. Compute metrics and log:
        self.log("train_loss", train_loss, on_step=True, on_epoch=False,  prog_bar=True, logger=False, sync_dist=False)
        for metric_name in self.train_metrics.keys():
            self.log(metric_name, getattr(self, metric_name)(y_logits, y), on_step=True, on_epoch=False, prog_bar=True, logger=False, sync_dist=False)

        return train_loss

    def training_epoch_end(self, outputs):
        for metric_name in self.train_metrics.keys():
            self.log(metric_name, getattr(self, metric_name).compute(), on_step=False, on_epoch=True, prog_bar=False, logger=True, sync_dist=True)
            getattr(self, metric_name).reset()

    def validation_step(self, batch, batch_idx):
        # 1. Forward pass:
        x, y = batch
        y_logits = self.forward(x)

        # 2. Compute loss:
        val_loss = self.loss(y_logits, y)
        
        # 3. Compute metrics and log:
        self.log('val_loss', val_loss, on_step=True, on_epoch=False,  prog_bar=False, logger=False, sync_dist=False)
        for metric_name in self.validation_metrics.keys():
            getattr(self, metric_name).update(y_logits, y)

    def validation_epoch_end(self, outputs):
        for metric_name in self.validation_metrics.keys():
            self.log(metric_name, getattr(self, metric_name).compute(), on_step=False, on_epoch=True, prog_bar=False, logger=True, sync_dist=True)
            getattr(self, metric_name).reset()

    def test_step(self, batch, batch_idx):
        x, y, name = batch
        y_logits = self.forward(x)
        y_pred = 1 - F.softmax(y_logits.double(), dim=1)[:,0]
        
        result = pl.EvalResult()
        result.write('preds_logit', y_pred, filename='predictions.txt')
        result.write('label', torch.argmax(y, dim=1), filename='predictions.txt')
        result.write('name', list(name), filename='predictions.txt')
        return result
        
    def configure_optimizers(self):

        optimizer = get_optimizer(self.args.optimizer.name)
        
        optimizer_kwargs = {'momentum': 0.9} if self.args.optimizer.name == 'sgd' else {'eps': self.args.optimizer.eps}
        
        param_optimizer = list(self.net.named_parameters())
        
        if self.args.optimizer.decay_not_bias_norm:
            no_decay = ['bias', 'norm.bias', 'norm.weight', 'fc.weight', 'fc.bias']
        else:
            no_decay = []
        optimizer_grouped_parameters = [
            {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': self.args.optimizer.weight_decay},
            {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
            ] 
        optimizer = optimizer(optimizer_grouped_parameters, 
                              lr=self.args.optimizer.lr, 
                              **optimizer_kwargs)
        
        train_len = len(self.trainer.datamodule.train_dataset)
        #print("######### Training len", train_len)
        batch_size = self.args.training.batch_size
        #print("########## Batch size", batch_size)

        if self.args.optimizer.lr_scheduler_name == 'cos':
            scheduler_kwargs = {'T_max': self.args.training.epochs*train_len//len(self.args.training.gpus)//batch_size,
                                'eta_min':self.args.optimizer.lr/50}

        elif self.args.optimizer.lr_scheduler_name == 'onecycle':
            scheduler_kwargs = {'max_lr': self.args.optimizer.lr, 'epochs': self.args.training.epochs,
                                'steps_per_epoch':train_len//len(self.args.training.gpus)//batch_size,
                                'pct_start':4.0/self.args.training.epochs,'div_factor':25,'final_div_factor':2}
                                #'div_factor':25,'final_div_factor':2}

        elif self.args.optimizer.lr_scheduler_name == 'multistep':
             scheduler_kwargs = {'milestones':[350]}

        elif self.args.optimizer.lr_scheduler_name == 'const':
            scheduler_kwargs = {'lr_lambda': lambda epoch: 1}
            
        scheduler = get_lr_scheduler(self.args.optimizer.lr_scheduler_name)
        scheduler_params, interval = get_lr_scheduler_params(self.args.optimizer.lr_scheduler_name, **scheduler_kwargs)
        scheduler = scheduler(optimizer, **scheduler_params)

        return [optimizer], [{'scheduler':scheduler, 'interval': interval, 'name': 'lr'}]