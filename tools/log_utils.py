import uuid
import os
import random
from wonderwords import RandomWord
import hashlib
from omegaconf import OmegaConf
import omegaconf
import sys

from pytorch_lightning.callbacks.model_checkpoint import ModelCheckpoint
from pytorch_lightning.loggers import TensorBoardLogger
from pytorch_lightning.loggers import WandbLogger
from pytorch_lightning.callbacks import LearningRateMonitor
from pytorch_lightning.utilities.distributed import _get_rank

def gen_random_run_name():
    """
    Adapted from https://github.com/wandb/lit_utils/blob/main/utils.py
    """
    r = RandomWord()
    name = "-".join(
            [r.word(word_min_length=3, word_max_length=8, include_parts_of_speech=["adjective"]) ,
            r.word(word_min_length=3, word_max_length=8, include_parts_of_speech=["noun"]),
            str(random.randint(0,9))
            ])

    return name

def get_data_hash(args, hash_func="md5"):
    string_tobehashed = args.dataset.data_path
    for class_key in args.dataset.desc.keys():
        string_tobehashed = string_tobehashed + args.dataset.desc[class_key].path +\
                            str(args.dataset.desc[class_key].label)

    if hash_func == "md5":
        result  = hashlib.md5(string_tobehashed.encode())
        hash = "ds_"+result.hexdigest()[:8]

    return hash

def get_name_hash(name):
    return hashlib.md5(name.encode()).hexdigest()[:8]

def dump_data_desc(args, dirpath):
    dataset_yaml = args.dataset
    OmegaConf.save(config=dataset_yaml, f=os.path.join(dirpath, "dataset_info.yaml"))

def setup_callbacks_loggers(args):

    if args.logging.project is None and args.ckpt.resume_from is None:
        args.logging.project = os.getcwd().split('/')[-1]
    elif args.ckpt.resume_from is not None:
        # TODO: is this robust?
        args.logging.project = args.ckpt.resume_from.split('/')[-6]

    if args.logging.eid is None and args.ckpt.resume_from is None:
        args.logging.eid = gen_random_run_name()
    elif args.ckpt.resume_from is not None:
        # TODO: is this robust?
        args.logging.eid = args.ckpt.resume_from.split('/')[-3]

    args.logging.path = os.path.join(os.path.expanduser("~"), args.logging.path)

    data_hash = get_data_hash(args)
    log_path = os.path.join(args.logging.path, args.logging.project,
                            data_hash, args.model.backbone)

    log_path = os.path.join(log_path, args.logging.eid)
    tb_dir = os.path.join(log_path, "tensorboard")
    if _get_rank() == 0:
        os.makedirs(tb_dir, exist_ok=True)
        dump_data_desc(args, os.path.join(args.logging.path, args.logging.project, data_hash))

    wandb_logger = WandbLogger(project=args.logging.project,
                               dir=log_path,
                               name=args.logging.eid,
                               entity=args.logging.wandb.team,
                               mode='online' if args.logging.wandb.activate==True else 'offline',
                               id=get_name_hash(args.logging.eid),
                               resume=get_name_hash(args.logging.eid) if args.ckpt.resume_from is not None else None)

    tb_logger = TensorBoardLogger(tb_dir, name="", version="")
    lr_logger = LearningRateMonitor(logging_interval='epoch')
    ckpt_callback = ModelCheckpoint(dirpath=os.path.join(log_path, 'checkpoints'),
                                    filename='epoch{epoch:02d}-val_wAUC{val/wAUC:.4f}',
                                    auto_insert_metric_name=False,
                                    save_top_k=5, save_last=True, monitor='val/wAUC', mode='max')

    return ckpt_callback, [wandb_logger, tb_logger], lr_logger
