# python3
# coding=utf-8
# Copyright 2020 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# This script was derived from the original downloader.py provided in
# in the following link:
#       https://raw.githubusercontent.com/openimages/dataset/master/downloader.py

import argparse
from concurrent import futures
import os
import sys
import json
import requests


import boto3
import botocore
import tqdm
import pandas as pd
import numpy as np
import cv2


BUCKET_NAME = "open-images-dataset"
BBOX_ANNOTATIONS_URL = "https://storage.googleapis.com/openimages/v6/oidv6-train-annotations-bbox.csv"
ANNOTATIONS_FILE = "oidv6-train-annotations-bbox.csv"
MAP_CLASSES_URL = "https://storage.googleapis.com/openimages/v5/class-descriptions-boxable.csv"
MAP_CLASSES_FILE = "class-descriptions-boxable.csv"
CHUNK_SIZE = 1024 * 8


def get_args():
    parser = argparse.ArgumentParser(
        description="Download OpenImages Calibration Images", add_help=True
    )
    parser.add_argument(
        "--dataset-dir",
        default="/open-calibration-images-v6",
        help="dataset download location",
    )
    parser.add_argument(
        "--classes",
        default=None,
        nargs="+",
        type=str,
        help="Classes to download. default to all classes",
    )
    parser.add_argument(
        "--output-labels",
        default="labels.json",
        type=str,
        help="Classes to download. default to all classes",
    )
    parser.add_argument(
        "--num-processes",
        default=5,
        type=int,
        help="Number of parallel processes to use (default is 5).",
    )
    parser.add_argument(
        "--calibration-file",
        default=None,
        type=str,
        help="List of ids to download",
    )
    args = parser.parse_args()
    return args


def extract_dims(path):
    image = cv2.imread(path)
    return image.shape[:2]


def export_to_coco(
    classes, class_map, annotations, image_list, dataset_path, output_path
):
    # General information
    info_ = {
        "dataset": "openimages_mlperf",
        "version": "v6",
    }
    # Licenses
    licenses_ = []
    # Categories
    categories_ = [
        {"id": i, "name": class_, "supercategory": None}
        for i, class_ in enumerate(classes)
    ]
    categories_map = pd.DataFrame(
        [(i, class_) for i, class_ in enumerate(classes)],
        columns=["category_id", "category_name"],
    )
    class_map = class_map.merge(
        categories_map,
        left_on="DisplayName",
        right_on="category_name",
        how="inner",
    )
    image_list = [i[1] for i in image_list]
    annotations = annotations[np.isin(annotations["ImageID"], image_list)]
    annotations = annotations.merge(class_map, on="LabelName", how="inner")
    annotations["image_id"] = pd.factorize(annotations["ImageID"].tolist())[0]
    annotations[["height", "width"]] = annotations.apply(
        lambda x: extract_dims(
            os.path.join(dataset_path, f"{x['ImageID']}.jpg")
        ),
        axis=1,
        result_type="expand",
    )
    # Images
    images_ = []
    for i, row in (
        annotations.groupby(["image_id", "ImageID"]).first().iterrows()
    ):
        id, ImageID = i
        images_.append(
            {
                "id": int(id + 1),
                "file_name": f"{ImageID}.jpg",
                "height": row["height"],
                "width": row["width"],
                "license": None,
                "coco_url": None,
            }
        )

    # Annotations
    annotations_ = []
    for i, row in annotations.iterrows():
        annotations_.append(
            {
                "id": int(i) + 1,
                "image_id": int(row["image_id"] + 1),
                "category_id": int(row["category_id"]),
                "bbox": [
                    row["XMin"] * row["width"],
                    row["YMin"] * row["height"],
                    (row["XMax"] - row["XMin"]) * row["width"],
                    (row["YMax"] - row["YMin"]) * row["height"],
                ],
                "area": (row["XMax"] - row["XMin"])
                * row["width"]
                * (row["YMax"] - row["YMin"])
                * row["height"],
                "iscrowd": 0,
                "IsOccluded": row["IsOccluded"],
                "IsInside": row["IsInside"],
                "IsDepiction": row["IsDepiction"],
                "IsTruncated": row["IsTruncated"],
                "IsGroupOf": row["IsGroupOf"],
            }
        )
    coco_annotations = {
        "info": info_,
        "licenses": licenses_,
        "categories": categories_,
        "images": images_,
        "annotations": annotations_,
    }
    with open(output_path, "w") as fp:
        json.dump(coco_annotations, fp)


def get_remote_file(url, dest_file, dest_folder):
    if os.path.exists(os.path.join(dest_folder, dest_file)):
        return True
    file_path = os.path.join(dest_folder, dest_file)
    r = requests.get(url, stream=True)
    if r.ok:
        with open(file_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    f.write(chunk)
                    f.flush()
                    os.fsync(f.fileno())
    else:
        raise Exception(f"Unable to download file at {url}")


def download_one_image(bucket, split, image_id, download_folder):
    try:
        bucket.download_file(
            f"{split}/{image_id}.jpg",
            os.path.join(download_folder, f"{image_id}.jpg"),
        )
    except botocore.exceptions.ClientError as exception:
        sys.exit(
            f"ERROR when downloading image `{split}/{image_id}`: {str(exception)}"
        )


def download_all_images(args, image_list):
    """Downloads all images specified in the input file."""
    bucket = boto3.resource(
        "s3",
        config=botocore.config.Config(signature_version=botocore.UNSIGNED),
    ).Bucket(BUCKET_NAME)

    download_folder = args.dataset_dir

    if not os.path.exists(download_folder):
        os.makedirs(download_folder)

    if not os.path.exists(os.path.join(download_folder, "annotations")):
        os.makedirs(os.path.join(download_folder, "annotations"))

    if not os.path.exists(
        os.path.join(download_folder, "calibration", "data")
    ):
        os.makedirs(os.path.join(download_folder, "calibration", "data"))

    try:
        print("Downloading annotations...")
        get_remote_file(
            BBOX_ANNOTATIONS_URL,
            ANNOTATIONS_FILE,
            os.path.join(download_folder, "annotations"),
        )
        annotations = pd.read_csv(
            os.path.join(download_folder, "annotations", ANNOTATIONS_FILE)
        )
        get_remote_file(
            MAP_CLASSES_URL,
            MAP_CLASSES_FILE,
            os.path.join(download_folder, "annotations"),
        )
        class_map = pd.read_csv(
            os.path.join(download_folder, "annotations", MAP_CLASSES_FILE),
            names=["LabelName", "DisplayName"],
        )
    except Exception as exception:
        sys.exit(exception)

    progress_bar = tqdm.tqdm(
        total=len(image_list), desc="Downloading images", leave=True
    )
    with futures.ThreadPoolExecutor(
        max_workers=args.num_processes
    ) as executor:
        all_futures = [
            executor.submit(
                download_one_image,
                bucket,
                split,
                image_id,
                os.path.join(download_folder, "calibration", "data"),
            )
            for (split, image_id) in image_list
        ]
        for future in futures.as_completed(all_futures):
            future.result()
            progress_bar.update(1)
    progress_bar.close()
    print("Converting annotations to COCO format...")
    export_to_coco(
        args.classes,
        class_map,
        annotations,
        image_list,
        os.path.join(download_folder, "calibration", "data"),
        os.path.join(download_folder, "annotations", args.output_labels),
    )


if __name__ == "__main__":
    args = get_args()
    if args.calibration_file is None:
        # Try to find the calibration file in case it was not provided
        repo_root = os.path.dirname(
            os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
        )
        calibration_file = os.path.join(
            repo_root,
            "calibration",
            "openimages",
            "openimages_cal_images_list.txt",
        )
    else:
        calibration_file = args.calibration_file
    with open(calibration_file, "r+") as f:
        lines = f.readlines()
        image_ids = [("train", line.split(".")[0]) for line in lines]

    download_all_images(args, image_ids)
