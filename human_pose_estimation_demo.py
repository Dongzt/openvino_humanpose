#!/usr/bin/env python3
"""
 Copyright (C) 2020-2021 Intel Corporation

 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at

      http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
"""

import logging
import sys
from argparse import ArgumentParser, SUPPRESS
from pathlib import Path
from time import perf_counter

import cv2
import numpy as np
from openvino.inference_engine import IECore

a = str(Path(__file__).resolve().parent / 'common/python')
sys.path.append(a)
print(a)
import models
import monitors
from images_capture import open_images_capture
from pipelines import get_user_config, AsyncPipeline
from performance_metrics import PerformanceMetrics
from helpers import resolution

logging.basicConfig(format='[ %(levelname)s ] %(message)s', level=logging.INFO, stream=sys.stdout)
log = logging.getLogger()



import math


def build_argparser():
    parser = ArgumentParser(add_help=False)
    args = parser.add_argument_group('Options')
    args.add_argument('-h', '--help', action='help', default=SUPPRESS, help='Show this help message and exit.')
    args.add_argument('-m', '--model', help='Required. Path to an .xml file with a trained model.',
                      required=True, type=Path)
    args.add_argument('-at', '--architecture_type', help='Required. Specify model\' architecture type.',
                      type=str, required=True, choices=('ae', 'higherhrnet', 'openpose'))
    args.add_argument('-i', '--input', required=True,
                      help='Required. An input to process. The input must be a single image, '
                           'a folder of images, video file or camera id.')
    args.add_argument('--loop', default=False, action='store_true',
                      help='Optional. Enable reading the input in a loop.')
    args.add_argument('-o', '--output', required=False,
                      help='Optional. Name of the output file(s) to save.')
    args.add_argument('-limit', '--output_limit', required=False, default=1000, type=int,
                       help='Optional. Number of frames to store in output. '
                            'If 0 is set, all frames are stored.')
    args.add_argument('-d', '--device', default='CPU', type=str,
                      help='Optional. Specify the target device to infer on; CPU, GPU, HDDL or MYRIAD is '
                           'acceptable. The demo will look for a suitable plugin for device specified. '
                           'Default value is CPU.')

    common_model_args = parser.add_argument_group('Common model options')
    common_model_args.add_argument('-t', '--prob_threshold', default=0.1, type=float,
                                   help='Optional. Probability threshold for poses filtering.')
    common_model_args.add_argument('--tsize', default=None, type=int,
                                   help='Optional. Target input size. This demo implements image pre-processing '
                                        'pipeline that is common to human pose estimation approaches. Image is first '
                                        'resized to some target size and then the network is reshaped to fit the input '
                                        'image shape. By default target image size is determined based on the input '
                                        'shape from IR. Alternatively it can be manually set via this parameter. Note '
                                        'that for OpenPose-like nets image is resized to a predefined height, which is '
                                        'the target size in this case. For Associative Embedding-like nets target size '
                                        'is the length of a short first image side.')

    infer_args = parser.add_argument_group('Inference options')
    infer_args.add_argument('-nireq', '--num_infer_requests', help='Optional. Number of infer requests',
                            default=0, type=int)
    infer_args.add_argument('-nstreams', '--num_streams',
                            help='Optional. Number of streams to use for inference on the CPU or/and GPU in throughput '
                                 'mode (for HETERO and MULTI device cases use format '
                                 '<device1>:<nstreams1>,<device2>:<nstreams2> or just <nstreams>).',
                            default='', type=str)
    infer_args.add_argument('-nthreads', '--num_threads', default=None, type=int,
                            help='Optional. Number of threads to use for inference on CPU (including HETERO cases).')

    io_args = parser.add_argument_group('Input/output options')
    io_args.add_argument('-no_show', '--no_show', help="Optional. Don't show output.", action='store_true')
    io_args.add_argument('--output_resolution', default=None, type=resolution,
                         help='Optional. Specify the maximum output window resolution '
                              'in (width x height) format. Example: 1280x720. '
                              'Input frame size used by default.')
    io_args.add_argument('-u', '--utilization_monitors', default='', type=str,
                         help='Optional. List of monitors to show initially.')

    debug_args = parser.add_argument_group('Debug options')
    debug_args.add_argument('-r', '--raw_output_message', help='Optional. Output inference results raw values showing.',
                            default=False, action='store_true')
    return parser

def find_angle(position,p1, p2, p3):
    '''
    ?????????????????????3??????p1-p2-p3???????????????
    :return: ??????
    '''
    x1, y1 = position[p1][1][0], position[p1][1][1]
    x2, y2 = position[p2][1][0], position[p2][1][1]
    x3, y3 = position[p3][1][0], position[p3][1][1]
    # ??????????????????????????????3??????p1-p2-p3??????p2?????????????????????0-180?????????
    angle = int(math.degrees(math.atan2(y1 - y2, x1 - x2) - math.atan2(y3 - y2, x3 - x2)))
    if angle < 0:
        angle = angle + 360
    if angle > 180:
        angle = 360 - angle
    return angle



def get_model(ie, args, aspect_ratio):
    if args.architecture_type == 'ae':
        model = models.HpeAssociativeEmbedding(ie, args.model, target_size=args.tsize, aspect_ratio=aspect_ratio,
                                               prob_threshold=args.prob_threshold)
    elif args.architecture_type == 'higherhrnet':
        model = models.HpeAssociativeEmbedding(ie, args.model, target_size=args.tsize, aspect_ratio=aspect_ratio,
                                               prob_threshold=args.prob_threshold, delta=0.5, padding_mode='center')
    elif args.architecture_type == 'openpose':
        model = models.OpenPose(ie, args.model, target_size=args.tsize, aspect_ratio=aspect_ratio,
                                prob_threshold=args.prob_threshold)
    else:
        raise RuntimeError('No model type or invalid model type (-at) provided: {}'.format(args.architecture_type))
    return model


default_skeleton = ((15, 13), (13, 11), (16, 14), (14, 12), (11, 12), (5, 11), (6, 12), (5, 6),
    (5, 7), (6, 8), (7, 9), (8, 10), (1, 2), (0, 1), (0, 2), (1, 3), (2, 4), (3, 5), (4, 6))

colors = (
        (255, 0, 0), (255, 0, 255), (170, 0, 255), (255, 0, 85),
        (255, 0, 170), (85, 255, 0), (255, 170, 0), (0, 255, 0),
        (255, 255, 0), (0, 255, 85), (170, 255, 0), (0, 85, 255),
        (0, 255, 170), (0, 0, 255), (0, 255, 255), (85, 0, 255),
        (0, 170, 255))


count = 0
dir = 0

def draw_poses(img, poses, point_score_threshold, output_transform, skeleton=default_skeleton, draw_ellipses=False):
    img = output_transform.resize(img)
    if poses.size == 0:
        return img
    stick_width = 4
    img_limbs = np.copy(img)
    for pose in poses:
        points = pose[:, :2].astype(np.int32)
        points = output_transform.scale(points)
        points_scores = pose[:, 2]
        # Draw joints.
        position = []
        relativeshoulder = []
        relativewaist = []
        for i, (p, v) in enumerate(zip(points, points_scores)):
            if v > point_score_threshold:
                cv2.circle(img, tuple(p), 1, colors[i], 2)
                #??????text????????????????????????
                text="%d"%i
                #?????????????????????????????????
                if int(text) == 5 or int(text) == 6:
                    relativeshoulder.append(p)
                if int(text) == 11 or int(text) == 12:
                    relativewaist.append(p)
                onenode = []
                #?????????????????????????????????????????????????????????????????????position?????????
                onenode.append(text)
                onenode.append(p)
                position.append(onenode)
                cv2.putText(img, text, tuple(p), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 0, 255),2)
        i = 0
        p1, p2, p3 = -1, -1, -1
        p4, p5, p6 = -1, -1, -1
        #?????????????????????????????????????????????????????????
        if len(relativeshoulder)==0 or len(relativewaist)==0:
            continue
        #????????????????????????????????????????????????????????????????????????????????????????????????p1???p4???p2???p3???????????????????????????p5???p6
        if relativeshoulder[0][0]<relativewaist[0][0]:
            for item in position:
                if int(item[0]) == 5:
                    p1 = p4 = i
                if int(item[0]) == 7:
                    p2 = i
                if int(item[0]) == 9:
                    p3 = i
                if int(item[0]) == 11:
                    p5 = i
                if int(item[0]) == 13:
                    p6 = i
                i += 1
        else:
            for item in position:
                if int(item[0]) == 6:
                    p1 = p4 = i
                if int(item[0]) == 8:
                    p2 = i
                if int(item[0]) == 10:
                    p3 = i
                if int(item[0]) == 12:
                    p5 = i
                if int(item[0]) == 14:
                    p6 = i
                i += 1
        #???????????????????????????????????????find_angle?????????????????????????????????
        if p1 >= 0 and p2 >= 0 and p3 >= 0 and p4 >= 0 and p5 >= 0 and p6 >= 0:
            angle1 = find_angle(position, p1, p2, p3)
            angle2 = find_angle(position, p4, p5, p6)
            h, w, c = img.shape
            global dir
            global count
            # ????????????90???????????????
            if angle1 <= 90 and angle2 >= 150 and angle2 <= 175:
                if dir == 0:
                    count = count + 0.5
                    dir = 1
            # ????????????125???????????????
            if angle1 >= 125 and angle2 >= 150 and angle2 <= 175:
                if dir == 1:
                    count = count + 0.5
                    dir = 0
            cv2.putText(img, str(int(count)), (w // 2, h // 2), cv2.FONT_HERSHEY_SIMPLEX, 10, (255, 0, 0), 20,
                        cv2.LINE_AA)
        # Draw limbs.
        for i, j in skeleton:
            if points_scores[i] > point_score_threshold and points_scores[j] > point_score_threshold:
                if draw_ellipses:
                    middle = (points[i] + points[j]) // 2
                    vec = points[i] - points[j]
                    length = np.sqrt((vec * vec).sum())
                    angle = int(np.arctan2(vec[1], vec[0]) * 180 / np.pi)
                    polygon = cv2.ellipse2Poly(tuple(middle), (int(length / 2), min(int(length / 50), stick_width)),
                                               angle, 0, 360, 1)
                    cv2.fillConvexPoly(img_limbs, polygon, colors[j])
                else:
                    cv2.line(img_limbs, tuple(points[i]), tuple(points[j]), color=colors[j], thickness=stick_width)
    cv2.addWeighted(img, 0.4, img_limbs, 0.6, 0, dst=img)
    return img


def print_raw_results(poses, scores):
    log.info('Poses:')
    for pose, pose_score in zip(poses, scores):
        pose_str = ' '.join('({:.2f}, {:.2f}, {:.2f})'.format(p[0], p[1], p[2]) for p in pose)
        log.info('{} | {:.2f}'.format(pose_str, pose_score))


def main():
    args = build_argparser().parse_args()
    metrics = PerformanceMetrics()
    log.info('Initializing Inference Engine...')
    ie = IECore()
    plugin_config = get_user_config(args.device, args.num_streams, args.num_threads)
    cap = open_images_capture(args.input, args.loop)
  
   
    start_time = perf_counter()
    frame = cap.read()
    if frame is None:
        raise RuntimeError("Can't read an image from the input")

    log.info('Loading network...')
    model = get_model(ie, args, frame.shape[1] / frame.shape[0])
    hpe_pipeline = AsyncPipeline(ie, model, plugin_config, device=args.device, max_num_requests=args.num_infer_requests)

    log.info('Starting inference...')
    hpe_pipeline.submit_data(frame, 0, {'frame': frame, 'start_time': start_time})
    next_frame_id = 1
    next_frame_id_to_show = 0

    output_transform = models.OutputTransform(frame.shape[:2], args.output_resolution)
    if args.output_resolution:
        output_resolution = output_transform.new_resolution
    else:
        output_resolution = (frame.shape[1], frame.shape[0])
    presenter = monitors.Presenter(args.utilization_monitors, 55,
                                   (round(output_resolution[0] / 4), round(output_resolution[1] / 8)))
    video_writer = cv2.VideoWriter()
    if args.output and not video_writer.open(args.output, cv2.VideoWriter_fourcc(*'MJPG'), cap.fps(),
            output_resolution):
        raise RuntimeError("Can't open video writer")

    print("To close the application, press 'CTRL+C' here or switch to the output window and press ESC key")
    while True:
        if hpe_pipeline.callback_exceptions:
            raise hpe_pipeline.callback_exceptions[0]
        # Process all completed requests
        results = hpe_pipeline.get_result(next_frame_id_to_show)
        if results:
            (poses, scores), frame_meta = results
            frame = frame_meta['frame']
            start_time = frame_meta['start_time']

            if len(poses) and args.raw_output_message:
                print_raw_results(poses, scores)

            presenter.drawGraphs(frame)
            frame = draw_poses(frame, poses, args.prob_threshold, output_transform)
            metrics.update(start_time, frame)
            if video_writer.isOpened() and (args.output_limit <= 0 or next_frame_id_to_show <= args.output_limit-1):
                video_writer.write(frame)
            next_frame_id_to_show += 1
            if not args.no_show:
                cv2.imshow('Pose estimation results', frame)
                key = cv2.waitKey(1)

                ESC_KEY = 27
                # Quit.
                if key in {ord('q'), ord('Q'), ESC_KEY}:
                    break
                presenter.handleKey(key)
            continue

        if hpe_pipeline.is_ready():
            # Get new image/frame
            start_time = perf_counter()
            frame = cap.read()
            if frame is None:
                break

            # Submit for inference
            hpe_pipeline.submit_data(frame, next_frame_id, {'frame': frame, 'start_time': start_time})
            next_frame_id += 1

        else:
            # Wait for empty request
            hpe_pipeline.await_any()

    hpe_pipeline.await_all()
    # Process completed requests
    for next_frame_id_to_show in range(next_frame_id_to_show, next_frame_id):
        results = hpe_pipeline.get_result(next_frame_id_to_show)
        while results is None:
            results = hpe_pipeline.get_result(next_frame_id_to_show)
        (poses, scores), frame_meta = results
        frame = frame_meta['frame']
        start_time = frame_meta['start_time']

        if len(poses) and args.raw_output_message:
            print_raw_results(poses, scores)

        presenter.drawGraphs(frame)
        frame = draw_poses(frame, poses, args.prob_threshold, output_transform)
        metrics.update(start_time, frame)
        if video_writer.isOpened() and (args.output_limit <= 0 or next_frame_id_to_show <= args.output_limit-1):
            video_writer.write(frame)
        if not args.no_show:
            cv2.imshow('Pose estimation results', frame)
            key = cv2.waitKey(1)

            ESC_KEY = 27
            # Quit.
            if key in {ord('q'), ord('Q'), ESC_KEY}:
                break
            presenter.handleKey(key)

    metrics.print_total()
    print(presenter.reportMeans())


if __name__ == '__main__':
    sys.exit(main() or 0)
