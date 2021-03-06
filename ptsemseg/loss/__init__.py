import copy
import logging
import functools

from ptsemseg.loss.loss import cross_entropy2d,multi_step_DiceLoss
from ptsemseg.loss.loss import my_cross_entropy2d
from ptsemseg.loss.loss import bootstrapped_cross_entropy2d
from ptsemseg.loss.loss import multi_scale_cross_entropy2d
from ptsemseg.loss.loss import multi_step_cross_entropy2d
from ptsemseg.loss.loss import my_multi_step_cross_entropy2d

logger = logging.getLogger('ptsemseg')

key2loss = {'cross_entropy': cross_entropy2d,
            'multi_step_DiceLoss': multi_step_DiceLoss,
            'my_cross_entropy': my_cross_entropy2d,
            'bootstrapped_cross_entropy': bootstrapped_cross_entropy2d,
            'multi_scale_cross_entropy': multi_scale_cross_entropy2d,
            'multi_step_cross_entropy': multi_step_cross_entropy2d,
            'my_multi_step_cross_entropy': my_multi_step_cross_entropy2d,}

def  get_loss_function(cfg):
    if cfg['training']['loss'] is None:
        logger.info("Using default cross entropy loss")
        return cross_entropy2d
    elif 'Dice' in cfg['training']['loss']['name']:
        return multi_step_DiceLoss()

    else:
        loss_dict = cfg['training']['loss']
        loss_name = loss_dict['name']
        loss_params = {k:v for k,v in loss_dict.items() if k != 'name'}

        if loss_name not in key2loss:
            raise NotImplementedError('Loss {} not implemented'.format(loss_name))

        logger.info('Using {} with {} params'.format(loss_name, 
                                                     loss_params))

        return functools.partial(key2loss[loss_name], **loss_params)
