import os
import yaml
import json
import re
from sys import getsizeof
from types import ModuleType, FunctionType
from gc import get_referents

import torch


# Custom objects know their class.
# Function objects seem to know way too much, including modules.
# Exclude modules as well.
BLACKLIST_FROM_SIZE = type, ModuleType, FunctionType  # from SO


def config_to_str(config):
    return yaml.dump(yaml.safe_load(json.dumps(config)))  # fuck yeah


class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def calc_gradient_norm(named_parameters):
    total_norm = 0.0
    for name, p in named_parameters:
        # print(name)
        param_norm = p.grad.data.norm(2)
        total_norm += param_norm.item() ** 2

    total_norm = total_norm ** (1. / 2)

    return total_norm


def normalize_transformation(feature_range):
    def _f(x):
        shape = x.shape

        m = x.min()
        M = x.max()

        x = (x - m) / (M - m) * (feature_range[1] - feature_range[0]) + feature_range[0]

        x = x.reshape(shape)  # original size
        return x

    return _f


def get_size(obj):
    """sum size of object & members."""

    if isinstance(obj, BLACKLIST_FROM_SIZE):
        raise TypeError('getsize() does not take argument of type: '+ str(type(obj)))

    seen_ids = set()
    size = 0
    objects = [obj]

    while objects:
        need_referents = []
        for obj in objects:
            if not isinstance(obj, BLACKLIST_FROM_SIZE) and id(obj) not in seen_ids:
                seen_ids.add(id(obj))
                size += getsizeof(obj)
                need_referents.append(obj)
        objects = get_referents(*need_referents)

    return size  # bytes
