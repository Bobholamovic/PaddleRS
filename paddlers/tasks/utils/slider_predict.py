# Copyright (c) 2022 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import os.path as osp
from abc import ABCMeta, abstractmethod
from collections import Counter, defaultdict

import numpy as np

import paddlers.utils.logging as logging


class Cache(metaclass=ABCMeta):
    @abstractmethod
    def get_block(self, i_st, j_st, h, w):
        pass


class SlowCache(Cache):
    def __init__(self):
        self.cache = defaultdict(Counter)

    def push_pixel(self, i, j, l):
        self.cache[(i, j)][l] += 1

    def push_block(self, i_st, j_st, h, w, data):
        for i in range(0, h):
            for j in range(0, w):
                self.push_pixel(i_st + i, j_st + j, data[i, j])

    def pop_pixel(self, i, j):
        self.cache.pop((i, j))

    def pop_block(self, i_st, j_st, h, w):
        for i in range(0, h):
            for j in range(0, w):
                self.pop_pixel(i_st + i, j_st + j)

    def get_pixel(self, i, j):
        winners = self.cache[(i, j)].most_common(1)
        winner = winners[0]
        return winner[0]

    def get_block(self, i_st, j_st, h, w):
        block = []
        for i in range(i_st, i_st + h):
            row = []
            for j in range(j_st, j_st + w):
                row.append(self.get_pixel(i, j))
            block.append(row)
        return np.asarray(block)


class ProbCache(Cache):
    def __init__(self, h, w, ch, cw, sh, sw, dtype=np.float32, order='c'):
        self.cache = None
        self.h = h
        self.w = w
        self.ch = ch
        self.cw = cw
        self.sh = sh
        self.sw = sw
        if not issubclass(dtype, np.floating):
            raise TypeError("`dtype` must be one of the floating types.")
        self.dtype = dtype
        order = order.lower()
        if order not in ('c', 'f'):
            raise ValueError("`order` other than 'c' and 'f' is not supported.")
        self.order = order

    def _alloc_memory(self, nc):
        if self.order == 'c':
            # Colomn-first order (C-style)
            #
            # <-- cw -->
            # |--------|---------------------|^    ^
            # |                              ||    | sh
            # |--------|---------------------|| ch v
            # |                              || 
            # |--------|---------------------|v
            # <------------ w --------------->
            self.cache = np.zeros((self.ch, self.w, nc), dtype=self.dtype)
        elif self.order == 'f':
            # Row-first order (Fortran-style)
            #
            # <-- sw -->
            # <---- cw ---->
            # |--------|---|^   ^
            # |        |   ||   |
            # |        |   ||   ch
            # |        |   ||   |
            # |--------|---|| h v
            # |        |   ||
            # |        |   ||
            # |        |   ||
            # |--------|---|v
            self.cache = np.zeros((self.h, self.cw, nc), dtype=self.dtype)

    def update_block(self, i_st, j_st, h, w, prob_map):
        if self.cache is None:
            nc = prob_map.shape[2]
            # Lazy allocation of memory
            self._alloc_memory(nc)
        self.cache[i_st:i_st + h, j_st:j_st + w] += prob_map

    def roll_cache(self):
        if self.order == 'c':
            self.cache = np.roll(self.cache, -self.sh, axis=0)
            self.cache[self.sh:self.ch, :] = 0
        elif self.order == 'f':
            self.cache = np.roll(self.cache, -self.sw, axis=1)
            self.cache[:, self.sw:self.cw] = 0

    def get_block(self, i_st, j_st, h, w):
        return np.argmax(self.cache[i_st:i_st + h, j_st:j_st + w], axis=2)


def slider_predict(predictor, img_file, save_dir, block_size, overlap,
                   transforms, invalid_value, merge_strategy):
    """
    Do inference using sliding windows.

    Args:
        predictor (object): Object that implements `predict()` method.
        img_file (str|tuple[str]): Image path(s).
        save_dir (str): Directory that contains saved geotiff file.
        block_size (list[int] | tuple[int] | int):
            Size of block. If `block_size` is list or tuple, it should be in 
            (W, H) format.
        overlap (list[int] | tuple[int] | int):
            Overlap between two blocks. If `overlap` is list or tuple, it should
            be in (W, H) format.
        transforms (paddlers.transforms.Compose|None): Transforms for inputs. If 
            None, the transforms for evaluation process will be used. 
        invalid_value (int): Value that marks invalid pixels in output image. 
            Defaults to 255.
        merge_strategy (str): Strategy to merge overlapping blocks. Choices are 
            {'keep_first', 'keep_last', 'vote', 'accum'}. 'keep_first' and 
            'keep_last' means keeping the values of the first and the last block in 
            traversal order, respectively. 'vote' means applying a simple voting 
            strategy when there are conflicts in the overlapping pixels. 'accum' 
            means determining the class of an overlapping pixel according to 
            accumulated probabilities.
    """

    try:
        from osgeo import gdal
    except:
        import gdal

    if isinstance(block_size, int):
        block_size = (block_size, block_size)
    elif isinstance(block_size, (tuple, list)) and len(block_size) == 2:
        block_size = tuple(block_size)
    else:
        raise ValueError(
            "`block_size` must be a tuple/list of length 2 or an integer.")
    if isinstance(overlap, int):
        overlap = (overlap, overlap)
    elif isinstance(overlap, (tuple, list)) and len(overlap) == 2:
        overlap = tuple(overlap)
    else:
        raise ValueError(
            "`overlap` must be a tuple/list of length 2 or an integer.")

    if merge_strategy not in ('keep_first', 'keep_last', 'vote', 'accum'):
        raise ValueError("{} is not a supported stragegy for block merging.".
                         format(merge_strategy))

    step = np.array(
        block_size, dtype=np.int32) - np.array(
            overlap, dtype=np.int32)
    if step[0] == 0 or step[1] == 0:
        raise ValueError("`block_size` and `overlap` should not be equal.")

    if isinstance(img_file, tuple):
        if len(img_file) != 2:
            raise ValueError("Tuple `img_file` must have the length of two.")
        # Assume that two input images have the same size
        src_data = gdal.Open(img_file[0])
        src2_data = gdal.Open(img_file[1])
        # Output name is the same as the name of the first image
        file_name = osp.basename(osp.normpath(img_file[0]))
    else:
        src_data = gdal.Open(img_file)
        file_name = osp.basename(osp.normpath(img_file))

    # Get size of original raster
    width = src_data.RasterXSize
    height = src_data.RasterYSize
    bands = src_data.RasterCount

    if block_size[0] > width or block_size[1] > height:
        raise ValueError("`block_size` should not be larger than image size.")

    driver = gdal.GetDriverByName("GTiff")
    if not osp.exists(save_dir):
        os.makedirs(save_dir)
    # Replace extension name with '.tif'
    file_name = osp.splitext(file_name)[0] + ".tif"
    save_file = osp.join(save_dir, file_name)
    dst_data = driver.Create(save_file, width, height, 1, gdal.GDT_Byte)

    # Set meta-information
    dst_data.SetGeoTransform(src_data.GetGeoTransform())
    dst_data.SetProjection(src_data.GetProjection())

    # Initialize raster with `invalid_value`
    band = dst_data.GetRasterBand(1)
    band.WriteArray(
        np.full(
            (height, width), fill_value=invalid_value, dtype="uint8"))

    if overlap == (0, 0) or block_size == (width, height):
        # When there is no overlap or the whole image is used as input, 
        # use 'keep_last' strategy as it introduces least overheads
        merge_strategy = 'keep_last'
    if merge_strategy == 'vote':
        logging.warning(
            "Currently, a naive Python-implemented cache is used for aggregating voting results. "
            "For higher performance in inferring large images, please set `merge_strategy` to 'keep_first', "
            "'keep_last', or 'accum'.")
        cache = SlowCache()
    elif merge_strategy == 'accum':
        cache = ProbCache(height, width, *block_size, *step)

    prev_yoff, prev_xoff = None, None

    for yoff in range(0, height, step[1]):
        for xoff in range(0, width, step[0]):
            xsize, ysize = block_size
            if xoff + xsize > width:
                xoff = width - xsize
            if yoff + ysize > height:
                yoff = height - ysize

            # Read and fill
            im = src_data.ReadAsArray(xoff, yoff, xsize, ysize).transpose(
                (1, 2, 0))

            if isinstance(img_file, tuple):
                im2 = src2_data.ReadAsArray(xoff, yoff, xsize, ysize).transpose(
                    (1, 2, 0))
                # Predict
                out = predictor.predict((im, im2), transforms)
            else:
                # Predict
                out = predictor.predict(im, transforms)

            pred = out['label_map'].astype('uint8')
            pred = pred[:ysize, :xsize]

            # Deal with overlapping pixels
            if merge_strategy == 'vote':
                cache.push_block(yoff, xoff, ysize, xsize, pred)
                pred = cache.get_block(yoff, xoff, ysize, xsize)
                pred = pred.astype('uint8')
                if prev_yoff is not None:
                    pop_h = yoff - prev_yoff
                else:
                    pop_h = 0
                if prev_xoff is not None:
                    if xoff < prev_xoff:
                        pop_w = xsize
                    else:
                        pop_w = xoff - prev_xoff
                else:
                    pop_w = 0
                cache.pop_block(prev_yoff, prev_xoff, pop_h, pop_w)
            elif merge_strategy == 'keep_first':
                rd_block = band.ReadAsArray(xoff, yoff, xsize, ysize)
                mask = rd_block != invalid_value
                pred = np.where(mask, rd_block, pred)
            elif merge_strategy == 'keep_last':
                pass
            elif merge_strategy == 'accum':
                prob = out['score_map']
                prob = prob[:ysize, :xsize]
                cache.update_block(0, yoff, ysize, xsize, prob)
                pred = cache.get_block(0, yoff, ysize, xsize)
                if xoff + step[0] >= width:
                    cache.roll_cache()

            # Write to file
            band.WriteArray(pred, xoff, yoff)
            dst_data.FlushCache()

            prev_xoff = xoff
        prev_yoff = yoff

    dst_data = None
    logging.info("GeoTiff file saved in {}.".format(save_file))
