import logging
import tensorflow as tf

from embedding_dropout import get_embedding_dropout_mask
from match_lstm.encoder import Encoder
from match_lstm.decoder import Decoder


class MatchLSTM(object):
    def __init__(self, embeddings, output_types, output_shapes, **kwargs):
        self.model_name = 'MatchLSTM'
        self.kwargs = kwargs

        self.bs = kwargs['batch_size']
        self.l1_reg_lambda = kwargs['l1_reg']
        self.l2_reg_lambda = kwargs['l2_reg']
        self.emb_keep_prob = kwargs['input_emb_keep_prob']
        self.embeddings = embeddings
        self.handle = tf.placeholder(tf.string, shape=[])
        self.iterator = tf.data.Iterator.from_string_handle(
            self.handle, output_types, output_shapes)
        self.encoder = Encoder(hidden_units=kwargs['rnn_hidden_units'],
                               input_keep_prob=kwargs['input_dropout_keep_prob'],
                               output_keep_prob=kwargs['output_dropout_keep_prob'],
                               state_keep_prob=kwargs['state_dropout_keep_prob'],)
        self.decoder = Decoder(hidden_units=kwargs['rnn_hidden_units'],
                               keep_prob=kwargs['dropout_keep_prob'],
                               input_keep_prob=kwargs['input_dropout_keep_prob'],
                               output_keep_prob=kwargs['output_dropout_keep_prob'],
                               state_keep_prob=kwargs['state_dropout_keep_prob'],)
        self.question_embedding = None
        self.context_embedding = None
        self.logger = logging.getLogger('phrase_level_qa.qa_system.MatchLSTM')

    def setup_model(self):
        with tf.variable_scope('MatchLSTM'):
            self.prepare_data()
            self.setup_placeholders()
            self.encoder.setup_placeholders()
            self.decoder.setup_placeholders()
            self.setup_embeddings()
            self.setup_system()
            self.setup_predictions()
            self.setup_loss()

    def setup_placeholders(self):
        self.is_train = tf.placeholder(tf.bool, name='is_train')

    def prepare_data(self):
        self.context_ids, self.question_ids, self.labels, self.qa_ids, _, _ = self.iterator.get_next()

        self.c_mask = tf.cast(self.context_ids, tf.bool)
        self.q_mask = tf.cast(self.question_ids, tf.bool)
        self.c_len = tf.reduce_sum(tf.cast(self.c_mask, tf.int32), axis=1)
        self.q_len = tf.reduce_sum(tf.cast(self.q_mask, tf.int32), axis=1)
        self.c_maxlen = tf.reduce_max(self.c_len)
        self.q_maxlen = tf.reduce_max(self.q_len)

        self.context_ids = tf.slice(self.context_ids, [0, 0], [self.bs, self.c_maxlen])
        self.question_ids = tf.slice(self.question_ids, [0, 0], [self.bs, self.q_maxlen])
        self.c_mask = tf.slice(self.c_mask, [0, 0], [self.bs, self.c_maxlen])
        self.q_mask = tf.slice(self.q_mask, [0, 0], [self.bs, self.q_maxlen])
        self.labels_start = tf.slice(self.labels[:, 0, :], [0, 0], [self.bs, self.c_maxlen])
        self.labels_end = tf.slice(self.labels[:, 1, :], [0, 0], [self.bs, self.c_maxlen])

    def setup_system(self):
        context, question = self.encoder.encode(
            (self.context_embedding, self.question_embedding),
            (self.c_len, self.q_len),
            is_train=self.is_train
        )

        logits, probs = self.decoder.decode(
            context, question, self.c_len, self.q_len,
            labels=self.labels, is_train=self.is_train)

        self.logits = logits
        self.probs = probs

    def setup_predictions(self):
        outer = tf.matmul(tf.expand_dims(tf.nn.softmax(self.logits[0]), axis=2),
                          tf.expand_dims(tf.nn.softmax(self.logits[1]), axis=1))
        outer = tf.matrix_band_part(outer, 0, 15)
        self.yp1 = tf.argmax(tf.reduce_max(outer, axis=2), axis=1)
        self.yp2 = tf.argmax(tf.reduce_max(outer, axis=1), axis=1)

    def get_reg_loss(self):
        train_vars = tf.get_collection(tf.GraphKeys.REGULARIZATION_LOSSES)
        regularizer = tf.contrib.layers.l1_l2_regularizer(
            scale_l1=self.l1_reg_lambda,
            scale_l2=self.l2_reg_lambda
        )
        if len(train_vars):
            reg_loss = tf.contrib.layers.apply_regularization(regularizer, train_vars)
        else:
            reg_loss = 0.
        return reg_loss

    def setup_loss(self):
        with tf.variable_scope("loss"):
            reg_loss = tf.cond(self.is_train, lambda: self.get_reg_loss(), lambda: 0.)

            self.loss_start = tf.nn.softmax_cross_entropy_with_logits_v2(
                logits=self.logits[0], labels=self.labels_start)
            self.loss_end = tf.nn.softmax_cross_entropy_with_logits_v2(
                logits=self.logits[1], labels=self.labels_end)

            self.loss = tf.reduce_mean(self.loss_start + self.loss_end, name='loss') + reg_loss

    def setup_embeddings(self):
        self.question_embedding, self.context_embedding = self.setup_word_embeddings()

    def setup_word_embeddings(self):
        # Embedding layer
        with tf.name_scope("embedding"):
            self.W_emb = tf.Variable(self.embeddings,
                                     trainable=self.kwargs['train_wemb'],
                                     name="W_emb",
                                     dtype=tf.float32)
            mask1 = get_embedding_dropout_mask(
                self.emb_keep_prob,
                [self.kwargs['word_vocab_size'], self.kwargs['word_emb_size']],
                self.question_ids,
                seed=self.kwargs['random_seed'],
                is_train=self.is_train
            )
            W1_drop = self.W_emb * mask1

            mask2 = get_embedding_dropout_mask(
                self.emb_keep_prob,
                [self.kwargs['word_vocab_size'], self.kwargs['word_emb_size']],
                self.context_ids,
                seed=self.kwargs['random_seed'],
                is_train=self.is_train
            )
            W2_drop = self.W_emb * mask2

            question_embedding = tf.nn.embedding_lookup(W1_drop, self.question_ids)
            context_embedding = tf.nn.embedding_lookup(W2_drop, self.context_ids)

        return question_embedding, context_embedding

