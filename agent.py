#coding=utf-8

import argparse
import json
import os
import pickle
import socket
from xmlrpc.server import SimpleXMLRPCServer, SimpleXMLRPCRequestHandler

import numpy as np
import tensorflow as tf
from matplotlib import pyplot as plt

import flask

import rico
from rico.image import convert_view_trees
from rico.touch_input import convert_gestures
from train.model import MultipleScreenModel
from train.utils import visualize_data

class RPCHandler(SimpleXMLRPCRequestHandler):
    def _dispatch(self, method, params):
        try:
            return self.server.funcs[method](*params)
        except:
            import traceback
            traceback.print_exc()
            raise

class DroidBotDataProcessor():
    def __init__(self, agent_config_json):
        rico_config_path = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                        "rico", "config.json")
        with open(rico_config_path, "r") as rico_config_file:
            self.rico_config_json = json.load(rico_config_file)

        train_config_path = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                        "train", "config.json")
        with open(train_config_path, "r") as train_config_file:
            self.train_config_json = json.load(train_config_file)

        self.origin_dim = self.rico_config_json["origin_dim"]
        self.downscale_dim = self.rico_config_json["downscale_dim"]
        self.frame_num = self.train_config_json["frame_num"]
        self.predicting_dim = self.train_config_json["predicting_dim"]
        self.total_interacts = self.train_config_json["total_interacts"]
        self.navigation_back_bounds_options = agent_config_json["navigation_back_bounds"]

    def __clean_view_tree(self, view_tree):
        view_tree["visible-to-user"] = view_tree["visible"]
        bounds = view_tree["bounds"]
        view_tree["bounds"] = [bounds[0][0], bounds[0][1], bounds[1][0], bounds[1][1]]
        view_tree["rel-bounds"] = view_tree["bounds"]
        for child in view_tree["children"]:
            self.__clean_view_tree(child)

    def __event_to_pos(self, event):
        event_type = event["event_type"]
        if "x" in event and "y" in event and event["x"] is not None and event["y"] is not None:
            return [[event["x"] / self.origin_dim[0],
                     event["y"] / self.origin_dim[1]]]
        elif event_type in ["touch", "long_touch", "scroll", "set_text"]:
            # get view center
            x = (event["view"]["bounds"][0][0] + event["view"]["bounds"][1][0]) / 2
            y = (event["view"]["bounds"][0][1] + event["view"]["bounds"][1][1]) / 2
            return [[x / self.origin_dim[0],
                     y / self.origin_dim[1]]]
        elif event_type == "key" and event["name"] == "back":
            # get back center
            x = (self.navigation_back_bounds[0] + self.navigation_back_bounds[2]) / 2
            x = (self.navigation_back_bounds[1] + self.navigation_back_bounds[3]) / 2
            return [[x / self.origin_dim[0],
                     y / self.origin_dim[1]]]
        else:
            # event without pos
            return []

    def __events_to_touchs(self, events):
        return [self.__event_to_pos(x) for x in events]

    def __compute_prob(self, x_min, x_max, y_min, y_max, event_type, heatmap, interact):
        if x_min >= x_max or y_min >= y_max:
            return 0.0
        prob_sum = np.sum(heatmap[x_min:x_max, y_min:y_max])
        weighted_sum = prob_sum / ((x_max-x_min)*(y_max-y_min))
        return interact[self.rico_config_json[event_type]] * weighted_sum

    def events_to_probs(self, events, heatmap, interact):
        event_probs = []
        for event in events:
            event_type = event["event_type"]
            event_prob = 0.0
            if event_type in ["touch", "long_touch", "scroll", "set_text", "key"]:
                if event_type == "key" and event["name"] != "back":
                    event_prob = 0.0

                if event_type == "key":
                    bounds = self.navigation_back_bounds
                else:
                    bounds = event["view"]["bounds"]
                    bounds = [bounds[0][0], bounds[0][1], bounds[1][0], bounds[1][1]]
                x_min = max(0, int(bounds[0] * self.downscale_ratio))
                y_min = max(0, int(bounds[1] * self.downscale_ratio))
                x_max = min(self.downscale_dim[0], int(bounds[2] * self.downscale_ratio))
                y_max = min(self.downscale_dim[1], int(bounds[3] * self.downscale_ratio))
                if event_type in ["touch", "key"]:
                    event_prob = self.__compute_prob(x_min, x_max, y_min, y_max, "interact_touch", heatmap, interact)
                elif event_type == "long_touch":
                    event_prob = self.__compute_prob(x_min, x_max, y_min, y_max, "interact_long_touch", heatmap, interact)
                elif event_type == "scroll":
                    event_prob = self.__compute_prob(x_min, x_max, y_min, y_max,
                                            "interact_swipe_%s" % (event["direction"].lower()),
                                            heatmap, interact)
                elif event_type == "set_text":
                    event_prob = self.__compute_prob(x_min, x_max, y_min, y_max, "interact_input_text", heatmap, interact)
            event_probs.append(event_prob)
        return event_probs

    def process(self, query_json):
        self.origin_dim = query_json["screen_res"]
        self.rico_config_json["origin_dim"] = query_json["screen_res"]
        self.downscale_ratio = self.rico_config_json["downscale_dim"][0] / query_json["screen_res"][0]
        self.navigation_back_bounds = self.navigation_back_bounds_options\
                                           ["%dx%d" % (query_json["screen_res"][1],
                                                       query_json["screen_res"][0])]
        for i in range(4):
            self.navigation_back_bounds[i] *= self.downscale_ratio

        view_trees = [{
            "activity": {"root": x}
        } for x in query_json["history_view_trees"]]

        # clean view trees
        for view_tree in view_trees:
            self.__clean_view_tree(view_tree["activity"]["root"])
        # padding
        view_trees = [None] * (self.frame_num - len(view_trees)) + view_trees
        # assemble images by view tree
        images = convert_view_trees(view_trees, self.rico_config_json)

        # assemble touch heatmaps
        history_events = query_json["history_events"]
        gestures = self.__events_to_touchs(history_events)
        # padding
        gestures = [[]] * (self.frame_num - 1 - len(gestures)) + gestures + [[]]
        # print(gestures)
        heats, _ = convert_gestures(gestures, self.rico_config_json)

        summed_image = [x + y for x, y in zip(images, heats)]

        stacked_image = np.stack(summed_image, axis=0)
        stacked_image[-1, :, :, -self.predicting_dim:] = 0.0
        stacked_image -= 0.5

        dummy_heat = np.zeros_like(stacked_image[:1,:,:,:1])
        dummy_interact = np.zeros((1, self.total_interacts))

        return stacked_image, dummy_heat, dummy_interact

class HumanoidAgent():
    def __init__(self, domain, config_json):
        self.domain = domain
        self.rpc_port = self.get_random_port()
        print("Serving at %s:%d" % (self.domain, self.rpc_port))
        self.server = SimpleXMLRPCServer((self.domain, self.rpc_port), RPCHandler)
        self.server.register_function(self.query, "query")

        train_config_path = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                        "train", "config.json")
        with open(train_config_path, "r") as train_config_file:
            self.train_config_json = json.load(train_config_file)

        self.model = MultipleScreenModel(self.train_config_json, training=False)
        self.saver = tf.train.Saver()
        self.sess = tf.Session()
        self.saver.restore(self.sess, config_json["model_path"])
        self.data_processor = DroidBotDataProcessor(config_json)

    def get_random_port(self):
        temp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        temp_sock.bind(("", 0))
        port = temp_sock.getsockname()[1]
        temp_sock.close()
        return port

    def query(self, query_json_str):
        query_json = json.loads(query_json_str)
        possible_events = query_json["possible_events"]
        image, heat, interact = self.data_processor.process(query_json)
        heatmap = self.sess.run(self.model.predict_heatmaps,
                                feed_dict=self.model.get_feed_dict(image, heat, interact))
        interact = self.sess.run(self.model.predict_interacts,
                                 feed_dict=self.model.get_feed_dict(image, heat, interact))
        """
        visualize_data(stacked_image[0] + 0.5)
        visualize_data(stacked_image[1] + 0.5)
        visualize_data(stacked_image[2] + 0.5)
        visualize_data(stacked_image[3] + 0.5)
        visualize_data(heatmap[0])
        print(interact[0])
        """
        # print(event_probs)
        # print(prob_idx)
        event_probs = self.data_processor.events_to_probs(possible_events, heatmap[0,:,:,0], interact[0])
        prob_idx = sorted(range(len(event_probs)), key=lambda k: event_probs[k], reverse=True)
        return json.dumps(prob_idx)

    def run(self):
        self.server.serve_forever()

class HumanoidTest():
    def __init__(self):
        pass

    def test_model(self):
        with open("config.json", "r") as f:
            config_json = json.load(f)
        frame_num = config_json["frame_num"]
        predicting_dim = config_json["predicting_dim"]
        total_interacts = config_json["total_interacts"]
        model = MultipleScreenModel(config_json, training=False)

        data_path = "/mnt/DATA_volume/lab_data/RICO/training_data/jp.naver.linecard.android.pickle"
        with open(data_path, "rb") as f:
            input_data = pickle.load(f)
        image_num = len(input_data["trace_0"])
        stacked_images = np.stack([np.zeros_like(input_data["trace_0"][0][0], dtype=np.float32)] * (frame_num - 1) + \
                                [x[0] for x in input_data["trace_0"]], axis=0)
        images = [stacked_images[i:i + frame_num].copy() for i in range(image_num)]
        # clear last heatmaps
        for image in images:
            image[frame_num - 1, :, :, -predicting_dim:] = 0.0
            image -= 0.5

        heatmaps = np.stack([x[0][:,:,-predicting_dim:]
                            for x in input_data["trace_0"]], axis=0)
        interacts = np.eye(total_interacts)[[x[1]["interact_type"] for x in input_data["trace_0"]]]

        saver = tf.train.Saver()
        with tf.Session() as sess:
            saver.restore(sess, "/mnt/DATA_volume/lab_data/RICO/training_log/model_11500.ckpt")
            for i in range(image_num):
                heatmap = sess.run(model.predict_heatmaps, feed_dict=model.get_feed_dict(images[i], heatmaps[i:i+1], interacts[i:i+1]))
                interact = sess.run(model.predict_interacts, feed_dict=model.get_feed_dict(images[i], heatmaps[i:i+1], interacts[i:i+1]))
                visualize_data(images[i][frame_num - 1] + 0.5)
                visualize_data(heatmap[0])
                print(interact[0])

def run(config_path):
    with open(config_path, "r") as config_file:
        config_json = json.load(config_file)

    # data_processor = DroidBotDataProcessor(config_json)
    # with open("/mnt/EXT_volume/projects_light/Humanoid/query.json", "r") as f:
    #     data_processor.process(json.load(f))
    agent = HumanoidAgent("localhost", config_json)
    agent.run()

def parse_args():
    parser = argparse.ArgumentParser(description="Humanoid agent")
    parser.add_argument("-c", action="store", dest="config_path",
                        required=True, help="path/to/config.json")
    options = parser.parse_args()
    return options

def main():
    opts = parse_args()
    run(opts.config_path)
    return

if __name__ == "__main__":
    main()