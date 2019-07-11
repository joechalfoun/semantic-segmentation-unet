import sys
if sys.version_info[0] < 3:
    print('Python3 required')
    sys.exit(1)

import os
# set the system environment so that the PCIe GPU ids match the Nvidia ids in nvidia-smi
os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"  # so the IDs match nvidia-smi

import argparse
import datetime
import numpy as np

import tensorflow as tf
tf_version = tf.__version__.split('.')
if int(tf_version[0]) != 2:
    print('Tensorflow 2.x.x required')
    sys.exit(1)

import unet_model
import imagereader


def train_model(output_folder, batch_size, reader_count, train_lmdb_filepath, test_lmdb_filepath, use_augmentation, number_classes, balance_classes, learning_rate, test_every_n_steps, early_stopping_count, devices):
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)

    # TODO add ability to reload a checkpoint or saved model to resume training

    mirrored_strategy = tf.distribute.MirroredStrategy(devices=devices)
    with mirrored_strategy.scope():

        global_batch_size = batch_size * mirrored_strategy.num_replicas_in_sync
        print('reader count = {}'.format(reader_count))
        # scale the number of I/O readers based on the GPU count
        reader_count = reader_count * mirrored_strategy.num_replicas_in_sync

        print('Setting up test image reader')
        test_reader = imagereader.ImageReader(test_lmdb_filepath, use_augmentation=False, shuffle=False, num_workers=reader_count, balance_classes=False, number_classes=number_classes)
        print('Test Reader has {} images'.format(test_reader.get_image_count()))

        print('Setting up training image reader')
        train_reader = imagereader.ImageReader(train_lmdb_filepath, use_augmentation=use_augmentation, shuffle=True, num_workers=reader_count, balance_classes=balance_classes, number_classes=number_classes)
        print('Train Reader has {} images'.format(train_reader.get_image_count()))

        try:  # if any errors happen we want to catch them and shut down the multiprocess readers
            print('Starting Readers')
            train_reader.startup()
            test_reader.startup()

            train_dataset = train_reader.get_tf_dataset()
            train_dataset = train_dataset.batch(global_batch_size).prefetch(reader_count)
            train_dataset = mirrored_strategy.experimental_distribute_dataset(train_dataset)

            test_dataset = test_reader.get_tf_dataset()
            test_dataset = test_dataset.batch(global_batch_size).prefetch(reader_count)
            test_dataset = mirrored_strategy.experimental_distribute_dataset(test_dataset)

            print('Creating model')
            model = unet_model.UNet(number_classes, global_batch_size, train_reader.get_image_size(), learning_rate)

            checkpoint = tf.train.Checkpoint(optimizer=model.get_optimizer(), model=model.get_keras_model())

            # print the model summary to file
            with open(os.path.join(output_folder, 'model.txt'), 'w') as summary_fh:
                print_fn = lambda x: print(x, file=summary_fh)
                model.get_keras_model().summary(print_fn=print_fn)
            tf.keras.utils.plot_model(model.get_keras_model(), os.path.join(output_folder, 'model.png'), show_shapes=True)
            tf.keras.utils.plot_model(model.get_keras_model(), os.path.join(output_folder, 'model.dot'), show_shapes=True)

            # train_epoch_size = train_reader.get_image_count()/batch_size
            train_epoch_size = test_every_n_steps
            test_epoch_size = test_reader.get_image_count() / batch_size

            test_loss = list()

            # Prepare the metrics.
            train_loss_metric = tf.keras.metrics.Mean('train_loss', dtype=tf.float32)
            train_acc_metric = tf.keras.metrics.CategoricalAccuracy('train_accuracy')
            test_loss_metric = tf.keras.metrics.Mean('test_loss', dtype=tf.float32)
            test_acc_metric = tf.keras.metrics.CategoricalAccuracy('test_accuracy')

            current_time = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
            train_log_dir = os.path.join(output_folder, 'tensorboard-' + current_time, 'train')
            if not os.path.exists(train_log_dir):
                os.makedirs(train_log_dir)
            test_log_dir = os.path.join(output_folder, 'tensorboard-' + current_time, 'test')
            if not os.path.exists(test_log_dir):
                os.makedirs(test_log_dir)

            train_summary_writer = tf.summary.create_file_writer(train_log_dir)
            test_summary_writer = tf.summary.create_file_writer(test_log_dir)

            epoch = 0
            print('Running Network')
            while True:  # loop until early stopping
                print('---- Epoch: {} ----'.format(epoch))

                # Iterate over the batches of the train dataset.
                for step, (batch_images, batch_labels) in enumerate(train_dataset):
                    if step > train_epoch_size:
                        break

                    inputs = (batch_images, batch_labels, train_loss_metric, train_acc_metric)
                    model.dist_train_step(mirrored_strategy, inputs)

                    print('Train Epoch {}: Batch {}/{}: Loss {} Accuracy = {}'.format(epoch, step, train_epoch_size, train_loss_metric.result(), train_acc_metric.result()))
                    with train_summary_writer.as_default():
                        tf.summary.scalar('loss', train_loss_metric.result(), step=int(epoch * train_epoch_size + step))
                        tf.summary.scalar('accuracy', train_acc_metric.result(), step=int(epoch * train_epoch_size + step))
                    train_loss_metric.reset_states()
                    train_acc_metric.reset_states()

                # Iterate over the batches of the test dataset.
                epoch_test_loss = list()
                for step, (batch_images, batch_labels) in enumerate(test_dataset):
                    if step > test_epoch_size:
                        break

                    inputs = (batch_images, batch_labels, test_loss_metric, test_acc_metric)
                    loss_value = model.dist_test_step(mirrored_strategy, inputs)

                    epoch_test_loss.append(loss_value.numpy())
                    # print('Test Epoch {}: Batch {}/{}: Loss {}'.format(epoch, step, test_epoch_size, loss_value))
                test_loss.append(np.mean(epoch_test_loss))

                print('Test Epoch: {}: Loss = {} Accuracy = {}'.format(epoch, test_loss_metric.result(), test_acc_metric.result()))
                with test_summary_writer.as_default():
                    tf.summary.scalar('loss', test_loss_metric.result(), step=int((epoch+1) * train_epoch_size))
                    tf.summary.scalar('accuracy', test_acc_metric.result(), step=int((epoch+1) * train_epoch_size))
                test_loss_metric.reset_states()
                test_acc_metric.reset_states()

                with open(os.path.join(output_folder, 'test_loss.csv'), 'w') as csvfile:
                    for i in range(len(test_loss)):
                        csvfile.write(str(test_loss[i]))
                        csvfile.write('\n')

                # determine if to record a new checkpoint based on best test loss
                if (len(test_loss) - 1) == np.argmin(test_loss):
                    # save tf checkpoint
                    print('Test loss improved: {}, saving checkpoint'.format(np.min(test_loss)))
                    # checkpoint.save(os.path.join(output_folder, 'checkpoint', "ckpt")) # does not overwrite
                    checkpoint.write(os.path.join(output_folder, 'checkpoint', "ckpt"))

                # determine early stopping
                CONVERGENCE_TOLERANCE = 1e-4
                print('Best Current Epoch Selection:')
                print('Test Loss:')
                print(test_loss)
                min_test_loss = np.min(test_loss)
                error_from_best = np.abs(test_loss - min_test_loss)
                error_from_best[error_from_best < CONVERGENCE_TOLERANCE] = 0
                best_epoch = np.where(error_from_best == 0)[0][0] # unpack numpy array, select first time since that value has happened
                print('Best epoch: {}'.format(best_epoch))

                if len(test_loss) - best_epoch > early_stopping_count:
                    break  # break the epoch loop
                epoch = epoch + 1

        finally: # if any erros happened during training, shut down the disk readers
            print('Shutting down train_reader')
            train_reader.shutdown()
            print('Shutting down test_reader')
            test_reader.shutdown()

    # restore the checkpoint and generate a saved model
    model = unet_model.UNet(number_classes, global_batch_size, train_reader.get_image_size(), learning_rate)
    checkpoint = tf.train.Checkpoint(optimizer=model.get_optimizer(), model=model.get_keras_model())
    checkpoint.restore(tf.train.latest_checkpoint(os.path.join(output_folder, 'checkpoint')))
    tf.saved_model.save(model.get_keras_model(), os.path.join(output_folder, 'saved_model'))


def main():
    # Setup the Argument parsing
    parser = argparse.ArgumentParser(prog='train_unet', description='Script which trains a unet model')

    parser.add_argument('--batch_size', dest='batch_size', type=int, help='training batch size', default=4)
    parser.add_argument('--number_classes', dest='number_classes', type=int, default=2)
    parser.add_argument('--learning_rate', dest='learning_rate', type=float, default=1e-4)
    parser.add_argument('--output_dir', dest='output_folder', type=str, help='Folder where outputs will be saved (Required)', required=True)
    parser.add_argument('--test_every_n_steps', dest='test_every_n_steps', type=int, help='number of gradient update steps to take between test epochs', default=100)
    parser.add_argument('--balance_classes', dest='balance_classes', type=int, help='whether to balance classes [0 = false, 1 = true]', default=0)
    parser.add_argument('--use_augmentation', dest='use_augmentation', type=int, help='whether to use data augmentation [0 = false, 1 = true]', default=1)

    parser.add_argument('--train_database', dest='train_database_filepath', type=str, help='lmdb database to use for (Required)', required=True)
    parser.add_argument('--test_database', dest='test_database_filepath', type=str, help='lmdb database to use for testing (Required)', required=True)
    parser.add_argument('--early_stopping', dest='early_stopping_count', type=int, help='Perform early stopping when the test loss does not improve for N epochs.', default=10)
    parser.add_argument('--reader_count', dest='reader_count', type=int, help='how many threads to use for disk I/O and augmentation per gpu', default=1)
    parser.add_argument('--devices', dest='devices', type=str, help='comma separated list of which compute devices to use', default='/gpu:0')

    # TODO add parameter to specify the devices to use for training

    args = parser.parse_args()
    batch_size = args.batch_size
    output_folder = args.output_folder
    number_classes = args.number_classes
    early_stopping_count = args.early_stopping_count
    train_lmdb_filepath = args.train_database_filepath
    test_lmdb_filepath = args.test_database_filepath
    learning_rate = args.learning_rate
    test_every_n_steps = args.test_every_n_steps
    balance_classes = args.balance_classes
    use_augmentation = args.use_augmentation
    reader_count = args.reader_count
    devices = args.devices
    devices = devices.split(',')

    print('Arguments:')
    print('batch_size = {}'.format(batch_size))
    print('number_classes = {}'.format(number_classes))
    print('learning_rate = {}'.format(learning_rate))
    print('test_every_n_steps = {}'.format(test_every_n_steps))
    print('balance_classes = {}'.format(balance_classes))
    print('use_augmentation = {}'.format(use_augmentation))

    print('train_database = {}'.format(train_lmdb_filepath))
    print('test_database = {}'.format(test_lmdb_filepath))
    print('output folder = {}'.format(output_folder))

    print('early_stopping count = {}'.format(early_stopping_count))
    print('reader_count = {}'.format(reader_count))
    print('devices = {}'.format(devices))

    train_model(output_folder, batch_size, reader_count, train_lmdb_filepath, test_lmdb_filepath, use_augmentation, number_classes, balance_classes, learning_rate, test_every_n_steps, early_stopping_count, devices)


if __name__ == "__main__":
    main()
