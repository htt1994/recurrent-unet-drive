import os
import sys
import yaml
import time
import shutil
import torch
# import visdom
import random
import argparse
import datetime
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
import torchvision.models as models
import matplotlib.pyplot as plt
from torch.utils import data
from tqdm import tqdm

from ptsemseg.models import get_model
from ptsemseg.models.utils import MergeParametric
from ptsemseg.loss import get_loss_function
from ptsemseg.loader import get_loader 
from ptsemseg.utils import get_logger
from ptsemseg.metrics import runningScore, averageMeter
from ptsemseg.augmentations import get_composed_augmentations
from ptsemseg.schedulers import get_scheduler
from ptsemseg.optimizers import get_optimizer

from tensorboardX import SummaryWriter

# from gpu_profile import gpu_profile


def weights_init(m):
    if isinstance(m, MergeParametric):
        print('initializing merge layer ...')
        pass
    elif isinstance(m, nn.Conv2d):
        print(m)
        nn.init.kaiming_normal_(m.weight.data)
        if m.bias is not None:
            nn.init.zeros_(m.bias)
    elif isinstance(m, nn.ConvTranspose2d):
        print(m)
        nn.init.kaiming_normal_(m.weight.data)
        if m.bias is not None:
            nn.init.zeros_(m.bias)


def train(cfg, writer, logger, args):
    
    # Setup seeds
    torch.manual_seed(cfg.get('seed', 1337))
    torch.cuda.manual_seed(cfg.get('seed', 1337))
    np.random.seed(cfg.get('seed', 1337))
    random.seed(cfg.get('seed', 1337))

    # Setup device
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    # device = torch.device('cuda')

    # Setup Augmentations
    augmentations = cfg['training'].get('augmentations', None)
    if cfg['data']['dataset'] in ['cityscapes']:
        augmentations = cfg['training'].get('augmentations',
                                            {'brightness': 63. / 255.,
                                             'saturation': 0.5,
                                             'contrast': 0.8,
                                             'hflip': 0.5,
                                             'rotate': 10,
                                             'rscalecropsquare': 713,
                                             })
        # augmentations = cfg['training'].get('augmentations',
        #                                     {'rotate': 10, 'hflip': 0.5, 'rscalecrop': 512, 'gaussian': 0.5})
    data_aug = get_composed_augmentations(augmentations)

    # Setup Dataloader
    data_loader = get_loader(cfg['data']['dataset'])
    data_path = cfg['data']['path']

    t_loader = data_loader(
        data_path,
        is_transform=True,
        split=cfg['data']['train_split'],
        img_size=(cfg['data']['img_rows'], cfg['data']['img_cols']),
        augmentations=data_aug)

    v_loader = data_loader(
        data_path,
        is_transform=True,
        split=cfg['data']['val_split'],
        img_size=(cfg['data']['img_rows'], cfg['data']['img_cols']),)

    n_classes = t_loader.n_classes
    trainloader = data.DataLoader(t_loader,
                                  batch_size=cfg['training']['batch_size'], 
                                  num_workers=cfg['training']['n_workers'], 
                                  shuffle=True)

    valloader = data.DataLoader(v_loader, 
                                batch_size=cfg['training']['batch_size'], 
                                num_workers=cfg['training']['n_workers'])

    # Setup Metrics
    running_metrics_val = runningScore(n_classes)

    # Setup Model
    model = get_model(cfg['model'], n_classes, args).to(device)
    model.apply(weights_init)
    print('sleep for 1 seconds')
    time.sleep(1)
    model = torch.nn.DataParallel(model, device_ids=range(torch.cuda.device_count()))
    # model = torch.nn.DataParallel(model, device_ids=(0, 1))
    print(model.device_ids)

    # Setup optimizer, lr_scheduler and loss function
    optimizer_cls = get_optimizer(cfg)
    optimizer_params = {k:v for k, v in cfg['training']['optimizer'].items() 
                        if k != 'name'}

    optimizer = optimizer_cls(model.parameters(), **optimizer_params)
    logger.info("Using optimizer {}".format(optimizer))

    scheduler = get_scheduler(optimizer, cfg['training']['lr_schedule'])

    loss_fn = get_loss_function(cfg)
    logger.info("Using loss {}".format(loss_fn))
    if 'multi_step' in cfg['training']['loss']['name']:
        my_loss_fn = loss_fn(scale_weight=cfg['training']['loss']['scale_weight'],
                             n_inp=2,
                             weight=None,
                             reduction='sum',
                             bkargs=args)
    elif 'Dice' in cfg['training']['loss']['name']:
        my_loss_fn = loss_fn()

    else:
        my_loss_fn = loss_fn(weight=None,reduction='sum', bkargs=args)

    start_iter = 0
    if cfg['training']['resume'] is not None:
        if os.path.isfile(cfg['training']['resume']):
            logger.info(
                "Loading model and optimizer from checkpoint '{}'".format(cfg['training']['resume'])
            )
            checkpoint = torch.load(cfg['training']['resume'])
            model.load_state_dict(checkpoint["model_state"])
            optimizer.load_state_dict(checkpoint["optimizer_state"])
            scheduler.load_state_dict(checkpoint["scheduler_state"])
            start_iter = checkpoint["epoch"]
            logger.info(
                "Loaded checkpoint '{}' (iter {})".format(
                    cfg['training']['resume'], checkpoint["epoch"]
                )
            )
        else:
            logger.info("No checkpoint found at '{}'".format(cfg['training']['resume']))

    val_loss_meter = averageMeter()
    time_meter = averageMeter()

    best_iou = -100.0
    i = start_iter
    flag = True

    while i <= cfg['training']['train_iters'] and flag:
        for (images, labels) in trainloader:

            # # miniBatch 图像显示check
            # bs=cfg['training']['batch_size']
            # imgs = images.numpy()
            # imgs = np.transpose(imgs, [0, 2, 3, 1]).astype(np.uint8)
            # f, axarr = plt.subplots(bs, 2)
            # for j in range(bs):
            #     axarr[j][0].imshow(imgs[j])
            #     axarr[j][1].imshow(labels.numpy()[j])
            # plt.show()


            i += 1
            start_ts = time.time()
            scheduler.step()
            model.train()
            images = images.to(device)
            labels = labels.to(device)

            optimizer.zero_grad()
            outputs = model(images)

            loss = my_loss_fn(torch.squeeze(outputs), labels)

            loss.backward()
            optimizer.step()

            # gpu_profile(frame=sys._getframe(), event='line', arg=None)

            time_meter.update(time.time() - start_ts)

            if (i + 1) % cfg['training']['print_interval'] == 0:
                fmt_str = "Iter [{:d}/{:d}]  Loss: {:.4f}  Time/Image: {:.4f}"
                print_str = fmt_str.format(i + 1,
                                           cfg['training']['train_iters'], 
                                           loss.item(),
                                           time_meter.avg / cfg['training']['batch_size'])

                print(print_str)
                logger.info(print_str)
                writer.add_scalar('loss/train_loss', loss.item(), i+1)
                time_meter.reset()

            if (i + 1) % cfg['training']['val_interval'] == 0 or \
               (i + 1) == cfg['training']['train_iters']:
                model.eval()
                with torch.no_grad():
                    for i_val, (images_val, labels_val) in tqdm(enumerate(valloader)):
                        # miniBatch 图像显示check
                        bs=cfg['training']['batch_size']
                        imgs = images_val.numpy()
                        imgs = np.transpose(imgs, [0, 2, 3, 1]).astype(np.uint8)
                        f, axarr = plt.subplots(bs, 2)
                        for j in range(bs):
                            axarr[j][0].imshow(imgs[j])
                            axarr[j][1].imshow(labels_val.numpy()[j])
                        plt.show()


                        images_val = images_val.to(device)
                        labels_val = labels_val.to(device)

                        outputs = model(images_val)
                        val_loss = my_loss_fn(outputs, labels_val)

                        pred = outputs.data.max(1)[1].cpu().numpy()
                        gt = labels_val.data.cpu().numpy()

                        bs=cfg['training']['batch_size']
                        outputs = outputs.cpu().numpy()
                        f, axarr = plt.subplots(bs, 2)
                        for j in range(bs):
                            axarr[j][0].imshow(outputs[j][0])
                            axarr[j][1].imshow(gt[j])
                        plt.show()                        

                        running_metrics_val.update(gt, pred)
                        val_loss_meter.update(val_loss.item())

                writer.add_scalar('loss/val_loss', val_loss_meter.avg, i+1)
                logger.info("Iter %d Loss: %.4f" % (i + 1, val_loss_meter.avg))

                score, class_iou,class_f1 = running_metrics_val.get_scores()
                for k, v in score.items():
                    print(k, v)
                    logger.info('{}: {}'.format(k, v))
                    writer.add_scalar('val_metrics/{}'.format(k), v, i+1)

                for k, v in class_iou.items():
                    logger.info('{}: {}'.format(k, v))
                    writer.add_scalar('val_metrics/cls_{}'.format(k), v, i+1)

                val_loss_meter.reset()
                running_metrics_val.reset()

                if score["Mean IoU : \t"] >= best_iou:
                    best_iou = score["Mean IoU : \t"]
                    state = {
                        "epoch": i + 1,
                        "model_state": model.state_dict(),
                        "optimizer_state": optimizer.state_dict(),
                        "scheduler_state": scheduler.state_dict(),
                        "best_iou": best_iou,
                    }
                    save_path = os.path.join(writer.file_writer.get_logdir(),
                                             "{}_{}_best_model.pkl".format(
                                                 cfg['model']['arch'],
                                                 cfg['data']['dataset']))
                    torch.save(state, save_path)

            if (i + 1) == cfg['training']['train_iters']:
                flag = False
                break


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="config")
    parser.add_argument(
        "--config",
        nargs="?",
        type=str,
        default="configs/dataset/drive.yml",
        help="Configuration file to use"
    )

    args = parser.parse_args()
    with open(args.config) as fp:
        cfg = yaml.load(fp)

    args.device = "cuda" if torch.cuda.is_available() else "cpu"
    # cfg['model']['arch'] = 'pspnet'
    # cfg['training']['loss']['name'] = 'my_multi_step_cross_entropy'
    # cfg['training']['optimizer']['lr'] = 1.0e-6

    cfg['model']['arch'] = 'unet'
    cfg['training']['loss']['name'] = 'DiceLoss'
    # cfg['training']['optimizer']['lr'] = 1.0e-5

    if 'multi_step' in cfg['training']['loss']:
        cfg['training']['loss']['scale_weight'] = 0.4
    cfg['training']['loss']['bkargs'] = args

    run_id = random.randint(1, 100000)
    logdir = os.path.join('runs', os.path.basename(args.config)[:-4], cfg['model']['arch'], str(run_id))
    writer = SummaryWriter(log_dir=logdir)

    print('RUNDIR: {}'.format(logdir))
    shutil.copy(args.config, logdir)

    logger = get_logger(logdir)
    logger.info('Let the games begin')

    # sys.settrace(gpu_profile)
    train(cfg, writer, logger, args)
