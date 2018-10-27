#!/usr/bin/env python3
# Copyright 2018 The Texar Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Attentional Seq2seq.
"""
from __future__ import absolute_import
from __future__ import print_function
from __future__ import division

#pylint: disable=invalid-name, too-many-arguments, too-many-locals

import importlib
import os
import tensorflow as tf
import texar as tx

from nltk.translate.bleu_score import corpus_bleu

flags = tf.flags

flags.DEFINE_string("config_train", "config_train",
                    "The training config.")
flags.DEFINE_string("config_model", "config_model", "The model config.")
flags.DEFINE_string("config_data", "config_data_iwslt14_de-en",
                    "The dataset config.")
flags.DEFINE_string("expr_name", "iwslt14_de-en", "The experiment name. "
                    "Also used as the directory name of run.")
flags.DEFINE_integer("pretrain_epochs", 10000, "Number of pretraining epochs.")
flags.DEFINE_string("stage", "xe0", "stage.")
flags.DEFINE_boolean("reinitialize_optimizer", False, "Whether to reinitialize "
                     "optimizer state before training.")

FLAGS = flags.FLAGS

config_train = importlib.import_module(FLAGS.config_train)
config_model = importlib.import_module(FLAGS.config_model)
config_data = importlib.import_module(FLAGS.config_data)
expr_name = FLAGS.expr_name
pretrain_epochs = FLAGS.pretrain_epochs
stage = FLAGS.stage
reinitialize_optimizer = FLAGS.reinitialize_optimizer
mask_patterns = config_train.mask_patterns

d = config_train.train_xe["optimizer"]["kwargs"]
if stage.startswith("xe"):
    d["learning_rate"] = d["learning_rate"][int(stage[2:])]
else:
    d["learning_rate"] = d["learning_rate"][-1]


def get_scope_by_name(tensor):
    return tensor.name[: tensor.name.rfind('/') + 1]


def build_model(batch, train_data):
    """Assembles the seq2seq model.
    """
    source_embedder = tx.modules.WordEmbedder(
        vocab_size=train_data.source_vocab.size, hparams=config_model.embedder)

    encoder = tx.modules.BidirectionalRNNEncoder(
        hparams=config_model.encoder)

    enc_outputs, _ = encoder(source_embedder(batch['source_text_ids']))

    target_embedder = tx.modules.WordEmbedder(
        vocab_size=train_data.target_vocab.size, hparams=config_model.embedder)

    decoder = tx.modules.AttentionRNNDecoder(
        memory=tf.concat(enc_outputs, axis=2),
        memory_sequence_length=batch['source_length'],
        vocab_size=train_data.target_vocab.size,
        hparams=config_model.decoder)

    # cross-entropy + teacher-forcing pretraining
    tf_outputs, _, _ = decoder(
        decoding_strategy='train_greedy',
        inputs=target_embedder(batch['target_text_ids'][:, :-1]),
        sequence_length=batch['target_length']-1)

    loss_xe = tx.losses.sequence_sparse_softmax_cross_entropy(
        labels=batch['target_text_ids'][:, 1:],
        logits=tf_outputs.logits,
        sequence_length=batch['target_length']-1)

    train_xe_op = tx.core.get_train_op(
        loss_xe,
        hparams=config_train.train_xe)

    # teacher mask + DEBLEU fine-tuning
    tm_helper = tx.modules.TeacherMaskSoftmaxEmbeddingHelper(
        # must not remove last token, since it may be used as mask
        inputs=batch['target_text_ids'],
        sequence_length=batch['target_length']-1,
        embedding=target_embedder,
        n_unmask=mask_patterns[0][0],
        n_mask=mask_patterns[0][1],
        tau=config_train.tau)

    tm_outputs, _, _ = decoder(
        helper=tm_helper)

    loss_debleu = tx.losses.debleu(
        labels=batch['target_text_ids'][:, 1:],
        probs=tm_outputs.sample_id,
        sequence_length=batch['target_length']-1)

    train_debleu_op = tx.core.get_train_op(
        loss_debleu,
        hparams=config_train.train_debleu)

    # start and end tokens
    start_tokens = tf.ones_like(batch['target_length']) * \
            train_data.target_vocab.bos_token_id
    end_token = train_data.target_vocab.eos_token_id

    # policy gradient with sampling average as baseline
    n_samples = config_train.n_samples

    def untile(a):
        return tf.reshape(a, tf.concat([[-1, n_samples], tf.shape(a)[1:]], -1))

    tiled_start_tokens = tf.reshape(
        tf.tile(tf.expand_dims(start_tokens, 1), [1, n_samples]), [-1])

    sp_outputs, _, sp_lengths = decoder(
        decoding_strategy='infer_sample',
        embedding=target_embedder,
        start_tokens=tiled_start_tokens,
        end_token=end_token,
        max_decoding_length=config_train.sample_max_decoding_length)

    sp_ids = sp_outputs.sample_id
    sp_ids, sp_lengths = map(untile, (sp_ids, sp_lengths))

    # inference: beam search decoding
    bs_outputs, _, _ = tx.modules.beam_search_decode(
        decoder_or_cell=decoder,
        embedding=target_embedder,
        start_tokens=start_tokens,
        end_token=end_token,
        beam_width=config_train.infer_beam_width,
        max_decoding_length=config_train.infer_max_decoding_length)

    return train_xe_op, train_debleu_op, tm_helper, bs_outputs


def main():
    """Entrypoint.
    """
    train_data = tx.data.PairedTextData(hparams=config_data.train)
    val_data = tx.data.PairedTextData(hparams=config_data.val)
    test_data = tx.data.PairedTextData(hparams=config_data.test)
    data_iterator = tx.data.FeedableDataIterator(
        {'train': train_data, 'val': val_data, 'test': test_data})

    data_batch = data_iterator.get_next()

    global_step = tf.train.create_global_step()

    train_xe_op, train_debleu_op, tm_helper, infer_outputs = \
        build_model(data_batch, train_data)

    train_xe_op_initializer, train_debleu_op_initializer = [
        tf.variables_initializer(
            tf.get_collection(
                tf.GraphKeys.GLOBAL_VARIABLES,
                scope=get_scope_by_name(train_op)),
            name=name)
        for train_op, name in [
            (train_xe_op, "train_xe_op_initializer"),
            (train_debleu_op, "train_debleu_op_initializer")]]

    summary_tm = [
        tf.summary.scalar('tm/n_unmask', tm_helper.n_unmask),
        tf.summary.scalar('tm/n_mask', tm_helper.n_mask)]
    summary_xe_op = tf.summary.merge(
        tf.get_collection(
            tf.GraphKeys.SUMMARIES,
            scope=get_scope_by_name(train_xe_op)),
        name='summary_xe')
    summary_debleu_op = tf.summary.merge(
        tf.get_collection(
            tf.GraphKeys.SUMMARIES,
            scope=get_scope_by_name(train_debleu_op)) + summary_tm,
        name='summary_debleu')

    saver = tf.train.Saver(max_to_keep=None)

    def _train_epoch(sess, summary_writer, train_op, summary_op, trigger):
        print('in _train_epoch')

        data_iterator.restart_dataset(sess, 'train')
        feed_dict = {
            tx.global_mode(): tf.estimator.ModeKeys.TRAIN,
            data_iterator.handle: data_iterator.get_handle(sess, 'train')
        }

        while True:
            try:
                loss, summary, step = sess.run(
                    (train_op, summary_op, global_step), feed_dict)

                summary_writer.add_summary(summary, step)

                if step % config_train.steps_per_eval == 0:
                    _eval_epoch(sess, summary_writer, 'val', trigger)

            except tf.errors.OutOfRangeError:
                break

        print('end _train_epoch')

    def _eval_epoch(sess, summary_writer, mode, trigger):
        print('in _eval_epoch with mode {}'.format(mode))

        data_iterator.restart_dataset(sess, mode)
        feed_dict = {
            tx.global_mode(): tf.estimator.ModeKeys.EVAL,
            data_iterator.handle: data_iterator.get_handle(sess, mode)
        }

        ref_hypo_pairs = []
        fetches = [
            data_batch['target_text'][:, 1:],
            infer_outputs.predicted_ids[:, :, 0]
        ]

        while True:
            try:
                target_texts_ori, output_ids = sess.run(fetches, feed_dict)
                target_texts = tx.utils.strip_special_tokens(
                    target_texts_ori.tolist(), is_token_list=True)
                output_texts = tx.utils.map_ids_to_strs(
                    ids=output_ids.tolist(), vocab=val_data.target_vocab,
                    join=False)

                ref_hypo_pairs.extend(
                    zip(map(lambda x: [x], target_texts), output_texts))

            except tf.errors.OutOfRangeError:
                break

        refs, hypos = zip(*ref_hypo_pairs)
        bleu = corpus_bleu(refs, hypos) * 100
        print('{} BLEU: {}'.format(mode, bleu))

        step = tf.train.global_step(sess, global_step)

        summary = tf.Summary()
        summary.value.add(tag='{}/BLEU'.format(mode), simple_value=bleu)
        summary_writer.add_summary(summary, step)
        summary_writer.flush()

        if trigger is not None:
            triggered, _ = trigger(step, bleu)
            if triggered:
                print('triggered!')

        print('end _eval_epoch')
        return bleu

    best_val_bleu = -1
    with tf.Session() as sess:
        def action_of_mask(mask_pattern):
            sess.run(train_debleu_op_initializer)
            tm_helper.assign_mask_pattern(sess, *mask_pattern)

        action = (action_of_mask(mask_pattern)
                  for mask_pattern in mask_patterns[1:])
        trigger = tx.utils.BestEverConvergenceTrigger(
            action,
            config_train.threshold_steps,
            config_train.minimum_interval_steps,
            default=None)

        sess.run(tf.global_variables_initializer())
        sess.run(tf.local_variables_initializer())
        sess.run(tf.tables_initializer())

        dir_model = os.path.join(expr_name, 'ckpt')
        dir_best = os.path.join(expr_name, 'ckpt-best')
        ckpt_model = os.path.join(dir_model, 'model.ckpt')
        ckpt_best = os.path.join(dir_best, 'model.ckpt')

        if os.path.exists(dir_model):
            ckpt_path = tf.train.latest_checkpoint(dir_model)
            print('restoring from {} ...'.format(ckpt_path))
            saver.restore(sess, ckpt_path)

            if reinitialize_optimizer:
                sess.run(train_xe_op_initializer)
                sess.run(train_debleu_op_initializer)

            trigger_path = '{}.trigger'.format(ckpt_path)
            if os.path.exists(trigger_path):
                with open(trigger_path, 'rb') as pickle_file:
                    trigger.restore_from_pickle(pickle_file)
            else:
                print('cannot find previous trigger state.')

            print('done.')

        summary_writer = tf.summary.FileWriter(
            os.path.join(expr_name, 'log'), sess.graph, flush_secs=30)

        epoch = 0
        while epoch < config_train.max_epochs:
            print('epoch #{}{}:'.format(
                epoch, ' ({})'.format(stage)))

            val_bleu = _eval_epoch(sess, summary_writer, 'val', trigger)
            test_bleu = _eval_epoch(sess, summary_writer, 'test', None)
            step = tf.train.global_step(sess, global_step)
            print('epoch: {}, step: {}, val bleu: {}, test bleu: {}'.format(
                epoch, step, val_bleu, test_bleu))

            if val_bleu > best_val_bleu:
                best_val_bleu = val_bleu
                print('update best val bleu: {}'.format(best_val_bleu))

                saved_path = saver.save(
                    sess, ckpt_best, global_step=step)

                if stage == 'debleu':
                    with open('{}.trigger'.format(saved_path), 'wb') as \
                            pickle_file:
                        trigger.save_to_pickle(pickle_file)

                print('saved to {}'.format(saved_path))

            train_op, summary_op, trigger_ = {
                'xe0': (train_xe_op, summary_xe_op, None),
                'xe1': (train_xe_op, summary_xe_op, None),
                'debleu': (train_debleu_op, summary_debleu_op, trigger)
            }[stage]
            _train_epoch(sess, summary_writer, train_op, summary_op, trigger_)
            epoch += 1

            step = tf.train.global_step(sess, global_step)
            saved_path = saver.save(sess, ckpt_model, global_step=step)

            if stage == 'debleu':
                with open('{}.trigger'.format(saved_path), 'wb') as pickle_file:
                    trigger.save_to_pickle(pickle_file)

            print('saved to {}'.format(saved_path))


if __name__ == '__main__':
    main()

