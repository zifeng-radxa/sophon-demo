#===----------------------------------------------------------------------===#
#
# Copyright (C) 2024 Sophgo Technologies Inc.  All rights reserved.
#
# SOPHON-DEMO is licensed under the 2-Clause BSD License except for the
# third-party components.
#
#===----------------------------------------------------------------------===#
import numpy as np
from PIL import Image
import cv2

def HWC3(x):
    assert x.dtype == np.uint8
    if x.ndim == 2:
        x = x[:, :, None]
    assert x.ndim == 3
    H, W, C = x.shape
    assert C == 1 or C == 3 or C == 4
    if C == 3:
        return x
    if C == 1:
        return np.concatenate([x, x, x], axis=2)
    if C == 4:
        color = x[:, :, 0:3].astype(np.float32)
        alpha = x[:, :, 3:4].astype(np.float32) / 255.0
        y = color * alpha + 255.0 * (1.0 - alpha)
        y = y.clip(0, 255).astype(np.uint8)
        return y

def resize_image(input_image, resolution = 512):
    H, W, C = input_image.shape
    H = float(H)
    W = float(W)
    k = float(resolution) / min(H, W)
    H *= k
    W *= k
    H = int(np.round(H / 64.0)) * 64
    W = int(np.round(W / 64.0)) * 64
    img = cv2.resize(input_image, (W, H), interpolation=cv2.INTER_LANCZOS4 if k > 1 else cv2.INTER_AREA)
    return img

def _prepare_scribble_image(controlnet_img, scribble_processor):
    W, H = controlnet_img.size
    if not isinstance(controlnet_img, np.ndarray):
        controlnet_img = np.array(controlnet_img, dtype=np.uint8)
    
    controlnet_img = HWC3(controlnet_img)
    controlnet_img = resize_image(controlnet_img, 512)

    assert controlnet_img.ndim == 3
    controlnet_img = controlnet_img[:, :, ::-1].copy()

    image_hed = controlnet_img.astype(float)
    image_hed = image_hed / 255.0
    image_hed = image_hed.transpose((2,0,1))[np.newaxis, ...]
    image_hed = [image_hed]
    edge = scribble_processor(image_hed)
    edge = (edge[0] * 255.0).clip(0, 255).astype(np.uint8)
    detected_map = edge[0]
    detected_map = HWC3(detected_map)

    detected_map = nms(detected_map, 127, 3.0)
    detected_map = cv2.GaussianBlur(detected_map, (0, 0), 3.0)
    detected_map[detected_map > 4] = 255
    detected_map[detected_map < 255] = 0

    detected_map = Image.fromarray(detected_map)
    detected_map = detected_map.resize((W, H))
    detected_map = detected_map.convert("RGB")
    # pil to numpy
    detected_map = np.array(detected_map).astype(np.float32) / 255.0
    detected_map = [detected_map]
    detected_map = np.stack(detected_map, axis = 0)

    # (batch, channel, height, width)
    detected_map = detected_map.transpose(0, 3, 1, 2)

    detected_map_copy = np.copy(detected_map)
    detected_map = np.concatenate((detected_map,detected_map_copy), axis = 0)
    return detected_map

def nms(x, t, s):
    x = cv2.GaussianBlur(x.astype(np.float32), (0, 0), s)

    f1 = np.array([[0, 0, 0], [1, 1, 1], [0, 0, 0]], dtype=np.uint8)
    f2 = np.array([[0, 1, 0], [0, 1, 0], [0, 1, 0]], dtype=np.uint8)
    f3 = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=np.uint8)
    f4 = np.array([[0, 0, 1], [0, 1, 0], [1, 0, 0]], dtype=np.uint8)

    y = np.zeros_like(x)

    for f in [f1, f2, f3, f4]:
        np.putmask(y, cv2.dilate(x, kernel=f) == x, x)

    z = np.zeros_like(y, dtype=np.uint8)
    z[y > t] = 255
    return z