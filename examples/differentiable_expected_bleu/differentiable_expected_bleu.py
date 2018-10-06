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

flags = tf.flags

flags.DEFINE_string("config_train", "config_train", "The training config.")
flags.DEFINE_string("config_model", "config_model", "The model config.")
flags.DEFINE_string("config_data", "config_iwslt14", "The dataset config.")

FLAGS = flags.FLAGS

config_train = importlib.import_module(FLAGS.config_train)
config_model = importlib.import_module(FLAGS.config_model)
config_data = importlib.import_module(FLAGS.config_data)


def get_data_loader(sess, fetches, feed_dict):
    while True:
        try:
            yield sess.run(fetches, feed_dict=feed_dict)
        except tf.errors.OutOfRangeError:
            break

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

    train_xe_op = tx.core.get_train_op(
        tx.losses.sequence_sparse_softmax_cross_entropy(
            labels=batch['target_text_ids'][:, 1:],
            logits=tf_outputs.logits,
            sequence_length=batch['target_length']-1),
        hparams=config_train.train_xe)

    # teacher mask + DEBLEU fine-tuning
    tm_helper = tx.modules.TeacherMaskSoftmaxEmbeddingHelper(
        inputs=batch['target_text_ids'][:, :-1],
        sequence_length=batch['target_length']-1,
        embedding=target_embedder,
        n_unmask=1,
        n_mask=0,
        tau=config_train.tau)

    tm_outputs, _, _ = decoder(
        helper=tm_helper)

    train_debleu_op = tx.core.get_train_op(
        tx.losses.differentiable_expected_bleu(
            #TODO: decide whether to include BOS
            labels=batch['target_text_ids'][:, 1:],
            probs=tm_outputs.sample_id,
            sequence_length=batch['target_length']-1),
        hparams=config_train.train_debleu)

    # inference: beam search decoding
    start_tokens = tf.ones_like(batch['target_length']) * \
            train_data.target_vocab.bos_token_id
    end_token = train_data.target_vocab.eos_token_id

    bs_outputs, _, _ = tx.modules.beam_search_decode(
        decoder_or_cell=decoder,
        embedding=target_embedder,
        start_tokens=start_tokens,
        end_token=end_token,
        beam_width=config_train.infer_beam_width,
        max_decoding_length=config_train.infer_max_decoding_length)

    return train_xe_op, train_debleu_op, bs_outputs


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

    train_xe_op, train_debleu_op, infer_outputs = \
        build_model(data_batch, train_data)

    merged_summary = tf.summary.merge_all()

    saver = tf.train.Saver(max_to_keep=None)

    def _train_epoch(sess, summary_writer):
        data_iterator.restart_dataset(sess, 'train')
        feed_dict = {
            tx.global_mode(): tf.estimator.ModeKeys.TRAIN,
            data_iterator.handle: data_iterator.get_handle(sess, 'train')
        }

        for loss, summary, step in get_data_loader(
                sess, (train_xe_op, merged_summary, global_step), feed_dict):
            summary_writer.add_summary(summary, step)
            if step % config_train.steps_per_eval == 0:
                _eval_epoch(sess, summary_writer, 'val')

    def _eval_epoch(sess, summary_writer, mode):
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
        for target_texts_ori, output_ids in \
                get_data_loader(sess, fetches, feed_dict):
            target_texts = tx.utils.strip_special_tokens(target_texts_ori)
            output_texts = tx.utils.map_ids_to_strs(
                ids=output_ids, vocab=val_data.target_vocab)

            ref_hypo_pairs.extend(
                zip(map(lambda x: [x], target_texts), output_texts))

        refs, hypos = zip(*ref_hypo_pairs)
        bleu = tx.evals.corpus_bleu_moses(list_of_references=refs,
                                          hypotheses=hypos)
        step = tf.train.global_step(sess, global_step)
        summary = tf.Summary()
        summary.value.add(tag='{}/BLEU'.format(mode), simple_value=bleu)
        summary_writer.add_summary(summary, step)
        return bleu

    best_val_bleu = -1
    with tf.Session() as sess:
        sess.run(tf.tables_initializer())
        ckpt_name = 'ckpt/model.ckpt'
        if os.path.exists('ckpt') and tf.train.checkpoint_exists(ckpt_name):
            print('restoring from {} ...'.format(ckpt_name))
            saver.restore(sess, ckpt_name)
        else:
            sess.run(tf.global_variables_initializer())
            sess.run(tf.local_variables_initializer())

        summary_writer = tf.summary.FileWriter('log', sess.graph)

        epoch = 0
        while epoch < config_train.max_epochs:
            val_bleu = _eval_epoch(sess, summary_writer, 'val')
            if val_bleu > best_val_bleu:
                best_val_bleu = val_bleu
                print('epoch: {}, step: {}, best val bleu: {}'.format(
                    epoch,
                    tf.train.global_step(sess, global_step),
                    best_val_bleu))
                saved_path = saver.save(sess, 'ckpt/best.ckpt')
                print('saved to {}'.format(saved_path))
            _train_epoch(sess, summary_writer)
            epoch += 1
            saved_path = saver.save(sess, 'ckpt/model.ckpt')
            print('saved to {}'.format(saved_path))


if __name__ == '__main__':
    main()

