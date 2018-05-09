from __future__ import print_function
import tensorflow as tf
import numpy as np
import os
import cv2
import sys
import glob
import time
import shutil
import argparse

sys.path.insert(0, 'svs_reader')
from slide import Slide

sys.path.insert(0, 'tfmodels')
import tfmodels

sys.path.insert(0, '.')
from unet import Inference

config = tf.ConfigProto()
config.gpu_options.allow_growth = True

PROCESS_MAG = 10
PROCESS_SIZE = 256
OVERSAMPLE = 1.1
BATCH_SIZE = 12
PRINT_ITER = 1000
SNAPSHOT_PATH = 'unet/10x/snapshots/unet.ckpt-61690'
RAM_DISK = '/dev/shm'

def preprocess_fn(img):
    img = img * (2/255.) -1
    return img.astype(np.float32)

def prob_output(svs):
    probs = svs.output_imgs['prob']
    ## Important to quantize to [0, 255] uint8!!
    probs *= 255.
    probs = probs.astype(np.uint8)
    return probs
    # return probs[:,:,:3]

def rgb_output(svs):
    rgb = svs.output_imgs['rgb']
    rgb += 1.0
    rgb *= (255. / 2.)
    # print 'fixed: ', rgb.shape, rgb.dtype, rgb.min(), rgb.max()
    return rgb[:,:,::-1]

def transfer_to_ramdisk(src, ramdisk = RAM_DISK):
    base = os.path.basename(src)
    dst = os.path.join(ramdisk, base)
    shutil.copyfile(src, dst)
    return dst

def main(slide_path, model, sess, out_dir):
    print('Working {}'.format(slide_path))
    svs = Slide(slide_path    = ramdisk_path,
                preprocess_fn = preprocess_fn,
                process_mag   = PROCESS_MAG,
                process_size  = PROCESS_SIZE,
                verbose = True,
                )
    svs.initialize_output('prob', dim=5)
    svs.initialize_output('rgb', dim=3)
    svs.print_info()
    PREFETCH = min(len(svs.place_list), 1024)

    def wrapped_fn(idx):
        try:
            coords = svs.tile_list[idx]
            img = svs._read_tile(coords)
            return img, idx
        except:
            return 0

    def read_region_at_index(idx):
        return tf.py_func(func = wrapped_fn,
                          inp  = [idx],
                          Tout = [tf.float32, tf.int64],
                          stateful = False)

    ds = tf.data.Dataset.from_generator(generator=svs.generate_index,
        output_types=tf.int64)
    ds = ds.map(read_region_at_index, num_parallel_calls=8)
    ds = ds.prefetch(PREFETCH)
    ds = ds.batch(BATCH_SIZE)

    iterator = ds.make_one_shot_iterator()
    img, idx = iterator.get_next()

    print('Processing {} tiles'.format(len(svs.tile_list)))
    tstart = time.time()
    n_processed = 0
    while True:
        try:
            tile, idx_ = sess.run([img, idx])
            output = model.inference(tile)
            svs.place_batch(output, idx_, 'prob')
            svs.place_batch(tile, idx_, 'rgb')

            n_processed += BATCH_SIZE
            if n_processed % PRINT_ITER == 0:
                print('[{:06d}] elapsed time [{:3.3f}]'.format(
                    n_processed, time.time() - tstart ))

        except tf.errors.OutOfRangeError:
            print('Finished')
            break

        except Exception as e:
            print('Caught exception at tiles {}'.format(idx_))
            print(e.__doc__)
            print(e.message)
            break

    dt = time.time()-tstart
    spt = dt / float(len(svs.tile_list))
    print('\nFinished. {:2.2f}min {:3.3f}s/tile\n'.format(dt/60., spt))
    print('\t {:3.3f} fps\n'.format(len(svs.tile_list) / dt))

    svs.make_outputs()
    prob_img = prob_output(svs)
    rgb_img = rgb_output(svs)
    svs.close()

    return prob_img, rgb_img


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--slide_dir')
    parser.add_argument('--out')

    args = parser.parse_args()
    slide_dir = args.slide_dir
    out_dir = args.out

    slide_list = glob.glob(os.path.join(slide_dir, '*svs'))
    print('Working on {} slides from {}'.format(len(slide_list), slide_dir))

    processed_list = glob.glob(os.path.join(out_dir, '*prob.npy'))
    print('Found {} processed slides.'.format(len(processed_list)))
    processed_base = [os.path.basename(x).replace('_prob.npy', '') for x in processed_list]

    slide_base = [os.path.basename(x).replace('.svs', '') for x in slide_list]
    slide_base_list = zip(slide_base, slide_list)
    slide_list = [lst for bas, lst in slide_base_list if bas not in processed_base]
    print('Trimmed processed slides. Working on {}'.format(len(slide_list)))

    print('out_dir: ', out_dir)
    with tf.Session(config=config) as sess:
        model = Inference(sess=sess, x_dims=[PROCESS_SIZE, PROCESS_SIZE, 3])
        # model.print_info()
        model.restore(SNAPSHOT_PATH)

        for slide_path in slide_list:
            ramdisk_path = transfer_to_ramdisk(slide_path)
            try:
                prob_img, rgb_img = main(ramdisk_path, model, sess, out_dir)
                outname_prob = os.path.basename(ramdisk_path).replace('.svs', '_prob.npy')
                outname_rgb = os.path.basename(ramdisk_path).replace('.svs', '_rgb.jpg')

                outpath =  os.path.join(out_dir, outname_prob)
                print('Writing {}'.format(outpath))
                np.save(outpath, prob_img)

                outpath =  os.path.join(out_dir, outname_rgb)
                print('Writing {}'.format(outpath))
                cv2.imwrite(outpath, rgb_img)

            except Exception as e:
                print('Caught exception')
                print(e.__doc__)
                print(e.message)
            finally:
                os.remove(ramdisk_path)
                print('Removed {}'.format(ramdisk_path))

    print('Done!')
