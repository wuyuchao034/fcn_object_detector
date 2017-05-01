#!/usr/bin/env python

import sys
import os
import rospy
import rosbag
import message_filters
import caffe
import math
from sensor_msgs.msg import Image
from geometry_msgs.msg import PolygonStamped as Rect
import numpy as np
import cv2 as cv
from cv_bridge import CvBridge
import matplotlib.pylab as plt

class BoundaryRefinement:
    def __init__(self):
        self.__net = None
        self.__transformer = None
        self.__transformer2 = None
        self.__im_width = None
        self.__im_height = None

        self.__bridge = CvBridge()
        
        self.__weights = rospy.get_param('~pretrained_weights', None)
        self.__model_proto = rospy.get_param('~deployment_prototxt', None)
        self.__device_id = rospy.get_param('device_id', 0)
        self.__prob_thresh = rospy.get_param('~detection_threshold', 0.8)  #! threshold for masking the detection
        self.__min_bbox_thresh = rospy.get_param('~min_boxes', 1) #! minimum bounding box
        self.__group_eps_thresh = rospy.get_param('~nms_eps', 0.2) #! bbox grouping        
        ##! temp
        # folder_path = '/home/krishneel/nvcaffe/jobs/tracknet/'
        # self.__model_proto = folder_path  + 'deploy.prototxt'  
        # self.__weights =  folder_path + '/snapshot_iter_40000.caffemodel'

        folder_path = '/home/krishneel/Documents/programs/GOTURN/nets/'
        self.__model_proto = folder_path  + 'tracker.prototxt'  
        self.__weights =  folder_path + 'models/pretrained_model/tracker.caffemodel'
        
        if self.is_file_valid():
            self.load_caffe_model()
            rospy.loginfo('Boundary Refining Node Loaded')
            
        self.__prev_image__ = None
        self.subscribe()


    def callback(self, image_msg, rect_msg):
        cv_img = None
        try:
            cv_img = self.__bridge.imgmsg_to_cv2(image_msg, "bgr8")
        except Exception as e:
            print (e)
            return
        
        if cv_img is None:
            return

        caffe.set_device(self.__device_id)
        caffe.set_mode_gpu()

        x1 = rect_msg.polygon.points[0].x
        y1 = rect_msg.polygon.points[0].y
        x2 = rect_msg.polygon.points[1].x
        y2 = rect_msg.polygon.points[1].y
        
        factor = 2.50
        tlx = int(x1 - (x2-x1)/factor)
        tly = int(y1 - (y2-y1)/factor)
        brx = int(x2 + (x2-x1)/factor)
        bry = int(y2 + (y2-y1)/factor)
        
        x1 = 0 if tlx < 0 else tlx
        y1 = 0 if tly < 0 else tly
        x2 = cv_img.shape[1] if brx > cv_img.shape[1] else brx
        y2 = cv_img.shape[0] if bry > cv_img.shape[0] else bry

        curr_roi = cv_img[y1:y2, x1:x2].copy()
        im_roi = curr_roi.copy()

        curr_roi = cv.resize(curr_roi, (self.__im_width, self.__im_height))
        
        if self.__prev_image__ is None:
            self.__prev_image__ = curr_roi.copy()
            return
        
        self.__net.blobs['image'].data[...] = self.__transformer.preprocess('image', curr_roi)
        self.__net.blobs['target'].data[...] = self.__transformer2.preprocess('target', self.__prev_image__)

        output = self.__net.forward()
        
        # probability_map = self.__net.blobs['coverage'].data[0]
        # bbox_map = self.__net.blobs['bboxes'].data[0]

        # propose_boxes, propose_cvgs, mask = self.gridbox_to_boxes(probability_map, bbox_map, 0.5)
        # object_boxes = self.vote_boxes(propose_boxes, propose_cvgs, mask)
        # object_boxes = np.asarray(object_boxes, dtype=np.float16)
        
        
        box_coord = self.__net.blobs['fc8'].data[0]
        box_coord /= 10.0
        box_coord[0] *= self.__im_width
        box_coord[1] *= self.__im_height  
        box_coord[2] *= self.__im_width
        box_coord[3] *= self.__im_height

        box_coord = self.resize_detection(im_roi.shape, box_coord)
        
        x1 = box_coord[0]
        y1 = box_coord[1]
        x2 = box_coord[2]
        y2 = box_coord[3]
        
        print box_coord
        
        ##! update the prev template
        self.__prev_image__ = curr_roi.copy()

        # print object_boxes.shape[0]
        # if object_boxes.shape[0] > 0:
        #     x1 = int(object_boxes[0][0])
        #     y1 = int(object_boxes[0][1])
        #     x2 = x1 + int(object_boxes[0][2])
        #     y2 = y1 + int(object_boxes[0][3])
        #     cv.rectangle(curr_roi, (int(x1), int(y1)), (int(x2), int(y2)), (0, 0, 255), 4)
        # # # cv.imshow("img", cv_img)


        cv.rectangle(im_roi, (int(x1), int(y1)), (int(x2), int(y2)), (0, 0, 255), 4)
        cv.namedWindow("img", cv.WINDOW_NORMAL)
        cv.imshow("img", im_roi)
        cv.waitKey(5)



    def vote_boxes(self, propose_boxes, propose_cvgs, mask):
        detections_per_image = []
        if not propose_boxes.any():
            return detections_per_image
        nboxes, weights = cv.groupRectangles(np.array(propose_boxes).tolist(),
                                            self.__min_bbox_thresh, self.__group_eps_thresh)
            
        if len(nboxes):
            for rect, weight in zip(nboxes, weights):
                if (rect[3] - rect[1]) >= 20:
                    confidence = math.log(weight[0])
                    detection = [rect[0], rect[1], rect[2], rect[3], confidence]
                    detections_per_image.append(detection)
                    
        return detections_per_image

    def gridbox_to_boxes(self, net_cvg, net_boxes, prob_thresh):
        im_sz_x = self.__im_width
        im_sz_y = self.__im_height
        stride = 16

        grid_sz_x = int(im_sz_x / stride)
        grid_sz_y = int(im_sz_y / stride)

        boxes = []
        cvgs = []

        cell_width = im_sz_x / grid_sz_x
        cell_height = im_sz_y / grid_sz_y

        cvg_val = net_cvg[0][0:grid_sz_y][0:grid_sz_x]

        mask = (cvg_val >= prob_thresh)
        coord = np.where(mask == 1)

        y = np.asarray(coord[0])
        x = np.asarray(coord[1])
    
        mx = x * cell_width
        my = y * cell_height

        x1 = (np.asarray([net_boxes[0][y[i]][x[i]] for i in xrange(x.size)]) + mx)
        y1 = (np.asarray([net_boxes[1][y[i]][x[i]] for i in xrange(x.size)]) + my)
        x2 = (np.asarray([net_boxes[2][y[i]][x[i]] for i in xrange(x.size)]) + mx)
        y2 = (np.asarray([net_boxes[3][y[i]][x[i]] for i in xrange(x.size)]) + my)
        
        boxes = np.transpose(np.vstack((x1, y1, x2, y2)))
        cvgs = np.transpose(np.vstack((x, y, np.asarray(
            [
                cvg_val[y[i]][x[i]] 
                for i in xrange(x.size)
            ]
        ))))
        return boxes, cvgs, mask

    def resize_detection(self, in_size, bbox):
        diffx = float(in_size[1])/float(self.__im_width)
        diffy = float(in_size[0])/float(self.__im_height)
        resize_bbox = bbox
        resize_bbox[0] = bbox[0] * diffx
        resize_bbox[1] = bbox[1] * diffy
        resize_bbox[2] = bbox[2] * diffx
        resize_bbox[3] = bbox[3] * diffy 
        return resize_bbox



    def subscribe(self):
        image_sub = message_filters.Subscriber('/camera/rgb/image_rect_color', Image)
        rect_sub = message_filters.Subscriber('/object_rect', Rect)
        ts = message_filters.TimeSynchronizer([image_sub, rect_sub], 10)
        ts.registerCallback(self.callback)

    def vis_square(self, data):
        data = (data - data.min()) / (data.max() - data.min())
        n = int(np.ceil(np.sqrt(data.shape[0])))
        padding = (((0, n ** 2 - data.shape[0]),
                    (0, 1), (0, 1)) + ((0, 0),) * (data.ndim - 3))
        data = np.pad(data, padding, mode='constant', constant_values=1)
        data = data.reshape((n, n) + data.shape[1:]).transpose((0, 2, 1, 3) +
                                                               tuple(range(4, data.ndim + 1)))
        data = data.reshape((n * data.shape[1], n * data.shape[3]) + data.shape[4:])
        plt.imshow(data); plt.axis('off')
        plt.pause(0.09)

    def load_caffe_model(self):
        rospy.loginfo('LOADING CAFFE MODEL..')
        self.__net = caffe.Net(self.__model_proto, self.__weights, caffe.TEST)
        
        self.__transformer = caffe.io.Transformer({'image': self.__net.blobs['image'].data.shape})
        self.__transformer.set_transpose('image', (2,0,1))
        self.__transformer.set_raw_scale('image', 1)
        self.__transformer.set_channel_swap('image', (2,1,0))

        self.__transformer2 = caffe.io.Transformer({'target': self.__net.blobs['target'].data.shape})
        self.__transformer2.set_transpose('target', (2,0,1))
        self.__transformer2.set_raw_scale('target', 1)
        self.__transformer2.set_channel_swap('target', (2,1,0))

        shape = self.__net.blobs['image'].data.shape
        self.__im_height = shape[2]
        self.__im_width = shape[3]

        self.__net.blobs['image'].reshape(1, 3, self.__im_height, self.__im_width)
        self.__net.blobs['target'].reshape(1, 3, self.__im_height, self.__im_width)

    def is_file_valid(self):
        if self.__model_proto is None or \
           self.__weights is None:
            rospy.logfatal('PROVIDE PRETRAINED MODEL! KILLING NODE...')
            return False
        
        is_file = lambda path : os.path.isfile(str(path))
        if  (not is_file(self.__model_proto)) or (not is_file(self.__weights)):
            rospy.logfatal('NOT SUCH FILES')
            return False

        return True

def main(argv):
    try:
        rospy.init_node('boundary_refinement', anonymous = False)
        br = BoundaryRefinement()
        rospy.spin()
    except rospy.ROSInterruptException:
        rospy.logfatal("ros error")
        pass

if __name__ == "__main__":
    main(sys.argv)
