#===----------------------------------------------------------------------===#
#
# Copyright (C) 2022 Sophgo Technologies Inc.  All rights reserved.
#
# SOPHON-DEMO is licensed under the 2-Clause BSD License except for the
# third-party components.
#
#===----------------------------------------------------------------------===#
import time
import os
import numpy as np
import argparse
import sophon.sail as sail
import cv2
import math
import logging
import json
logging.basicConfig(level=logging.INFO)

class SlowFast:
    def __init__(self, args):
        # init hyperparams
        self.max_video_length = 300
        self.step = 2
        # init Engine
        self.net = sail.Engine(args.bmodel, args.dev_id, sail.IOMode.SYSIO)
        self.graph_name = self.net.get_graph_names()[0]
        self.input_slow = self.net.get_input_names(self.graph_name)[0]
        self.input_fast = self.net.get_input_names(self.graph_name)[1]
        self.output_name = self.net.get_output_names(self.graph_name)[0]
        self.input_shape_slow = self.net.get_input_shape(self.graph_name, self.input_slow)
        self.input_shape_fast = self.net.get_input_shape(self.graph_name, self.input_fast)
        self.batch_size = self.input_shape_fast[0]
        self.frame_size_slow = self.input_shape_slow[2]
        self.frame_size_fast = self.input_shape_fast[2]
        self.size = self.input_shape_fast[3]
        self.output_shape = self.net.get_output_shape(self.graph_name, self.output_name)
        self.input_dtype= self.net.get_input_dtype(self.graph_name, self.input_fast)
        self.output_dtype = self.net.get_output_dtype(self.graph_name, self.output_name)
        self.handle = self.net.get_handle()
        self.output = sail.Tensor(self.handle, self.output_shape, self.output_dtype, True, True)
        self.output_tensors = {self.output_name: self.output}
        self.preprocess_time = 0.0
        self.inference_time = 0.0
        self.postprocess_time = 0.0
        self.decode_time = 0.0

    def softmax(self,x):
        exp_x = np.exp(x)
        return exp_x / np.sum(exp_x,keepdims=True)
    
    def center_crop(self, frame):
        width = frame.shape[1]
        height = frame.shape[0]
        if width > height:
            newheight = self.size
            newwidth = int(math.floor(float(width)/height*self.size))
        else:
            newwidth = self.size
            newheight = int(math.floor(float(height)/width*self.size))
        width_start = int(round((newwidth-self.size)/2))
        height_start = int(round((newheight-self.size)/2))
        frame = cv2.resize(frame, (newwidth,newheight), interpolation=cv2.INTER_LINEAR)
        newframe = frame[height_start:height_start+self.size,width_start:width_start+self.size,:]
        return newframe
    
    def decode(self, video_list):
        start_decode = time.time()
        input_frame_array_batch = []
        for video_path in video_list:
            cap = cv2.VideoCapture(video_path)
            frame_id = 0
            input_frame_array = []
            for i in range(self.max_video_length):
                ret, frame = cap.read()
                if ret == 0 or frame_id >= self.input_shape_fast[2]:
                    break
                if i % self.step == 0:
                    frame_id += 1
                    input_frame_array.append(frame)
            while len(input_frame_array) < self.input_shape_fast[2]:
                input_frame_array.append(input_frame_array[-1])
            input_frame_array_batch.append(input_frame_array)
        
        self.decode_time += time.time() - start_decode
        return input_frame_array_batch
    
    def preprocess(self, input_frame_array):
        input_numpy_array = []
        for counter, frame in enumerate(input_frame_array):
            frame2 = cv2.cvtColor(frame,cv2.COLOR_RGB2BGR)
            frame3 = (frame2/255.0 - 0.45)/0.225
            frame4 = self.center_crop(frame3)
            input_numpy_array.append(frame4)
        while len(input_numpy_array) < self.input_shape_fast[2]:
            input_numpy_array.append(input_numpy_array[-1])
        if self.input_dtype == sail.BM_FLOAT32:
            input_numpy_array = np.array(input_numpy_array).astype(np.float32)
        elif self.input_dtype == sail.BM_INT8:
            input_numpy_array = np.array(input_numpy_array).astype(np.int8)
        input_numpy_array = np.transpose(input_numpy_array, (3, 0, 1, 2))
        return input_numpy_array
          
    def __call__(self, input_frame_array_batch):
        vid_num = len(input_frame_array_batch)
        start_preprocess = time.time()
        if self.batch_size == 1:
            preprocessed_video_data = self.preprocess(input_frame_array_batch[0])
            preprocessed_video_data = np.expand_dims(preprocessed_video_data, axis=0)
        elif self.batch_size > 1:
            preprocessed_video_data_raw = []
            for input_frame_array in input_frame_array_batch:
                preprocessed_video = self.preprocess(input_frame_array)    
                preprocessed_video_data_raw.append(preprocessed_video)
            preprocessed_video_data = np.zeros(self.input_shape_fast)
            preprocessed_video_data[:vid_num] = np.stack(preprocessed_video_data_raw)
        self.preprocess_time += time.time() - start_preprocess
        start_inference = time.time()
        input_tensor_fast = sail.Tensor(self.handle, preprocessed_video_data)
        input_tensor_slow = sail.Tensor(self.handle, preprocessed_video_data[:, :, ::4, :, :])
        input_tensors = {self.input_fast: input_tensor_fast, self.input_slow: input_tensor_slow}
        self.net.process(self.graph_name, input_tensors, self.output_tensors)
        self.inference_time += time.time() - start_inference        
        output_tensor = self.output_tensors[self.output_name].asnumpy()
        
        start_postprocess = time.time()
        result_list = []
        for output_ in output_tensor[:vid_num]:
            preds = self.softmax(output_)
            pred_idx = np.argsort(preds, axis=0)[-5:][::-1]
            #print(pred_idx)
            result_list.append(pred_idx[0])
        self.postprocess_time += time.time() - start_postprocess
        return result_list
        
def main(args):
    # check params
    if not os.path.exists(args.input):
        raise FileNotFoundError('{} is not existed.'.format(args.input))
    if not os.path.exists(args.bmodel):
        raise FileNotFoundError('{} is not existed.'.format(args.bmodel))
    with open(args.classnames, 'r') as f:
        class_names = [line.strip('\n') for line in f.readlines()]
    # creat save path
    output_dir = "./results"
    if not os.path.exists(output_dir):
        os.mkdir(output_dir)
    # initialize net
    slowfast = SlowFast(args)
    batch_size = slowfast.batch_size
    if os.path.isdir(args.input):
        video_paths = []
        for fpathe,dirs,fs in os.walk(args.input):
            for f in fs:
                if f.split(".")[-1] in ['avi', 'mp4']:
                    video_paths.append(os.path.join(fpathe,f))
    else:
        raise TypeError("invalid input path, need directory!")
    res_dict = dict()
    cn = 0
    video_list_batch = []
    for video_path in video_paths:
        logging.info("Read video path:{} ".format(video_path))
        video_list_batch.append(video_path)
        if len(video_list_batch) == batch_size:
            cn += batch_size
            input_numpy_array_batch = slowfast.decode(video_list_batch)
            result_list = slowfast(input_numpy_array_batch)
            for i in range(len(result_list)):
                res_dict[os.path.split(video_list_batch[i])[-1]] = class_names[result_list[i]]
                logging.info("predict: {}".format(class_names[result_list[i]]))
            video_list_batch = []
    if video_list_batch:
        cn += batch_size
        input_numpy_array_batch = slowfast.decode(video_list_batch)
        result_list = slowfast(input_numpy_array_batch)
        for i in range(len(result_list)):
            res_dict[os.path.split(video_list_batch[i])[-1]] = class_names[result_list[i]]
            logging.info("predict: {}".format(class_names[result_list[i]]))
        video_list_batch = []
            
    json_name = os.path.split(args.bmodel)[-1] + "_opencv_python.json"
    with open(os.path.join(output_dir, json_name), 'w') as jf:
        json.dump(res_dict, jf, indent=4, ensure_ascii=False)
    logging.info("result saved in {}".format(os.path.join(output_dir, json_name)))
    
    decode_time = slowfast.decode_time / cn
    preprocess_time = slowfast.preprocess_time / cn
    inference_time = slowfast.inference_time / cn
    postprocess_time = slowfast.postprocess_time / cn
    logging.info("decode_time(ms): {:.2f}".format(decode_time * 1000))
    logging.info("preprocess_time(ms): {:.2f}".format(preprocess_time * 1000))
    logging.info("inference_time(ms): {:.2f}".format(inference_time * 1000))
    logging.info("postprocess_time(ms): {:.2f}".format(postprocess_time * 1000))
def argsparser():
    parser = argparse.ArgumentParser(prog=__file__)
    parser.add_argument('--input', type=str, default='../datasets/test', help='path of input')
    parser.add_argument('--bmodel', type=str, default='../models/BM1684X/slowfast_bm1684x_fp32_1b.bmodel', help='path of bmodel')
    parser.add_argument('--dev_id', type=int, default=0, help='dev id')
    parser.add_argument('--classnames', type=str, default='../datasets/kinetics_classnames.txt', help='path of names')
    args = parser.parse_args()
    return args

if __name__ == "__main__":
    args = argsparser()
    main(args)
    print('all done.')
        
