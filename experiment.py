import numpy as np
import pandas as pd
import torch.nn as nn
import torch.nn.functional as F

import argparse
import datetime
import json
import os
import re
import torch 
import transformers

from collections import Counter, defaultdict
from tqdm import trange, tqdm
from transformers import BertTokenizer, BertModel, BertForQuestionAnswering

from eval_squad import *
from models.QAModels import *
from models.utils import *
from utils import *

# set random seeds to reproduce results
np.random.seed(42)
random.seed(42)
torch.manual_seed(42)
    
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--finetuning',  type=str, default='SQuAD',
            help='If SQuAD, fine tune on SQuAD only; if SubjQA, fine tune on SubjQA only; if combined, fine tune on both SQuAD and SubjQA simultaneously.')
    parser.add_argument('--version',  type=str, default='train',
            help='If train, then train model on train set(s); if test, then evaluate model on test set(s).')
    parser.add_argument('--multitask', action='store_true',
            help='If provided, MTL instead of STL setting.')
    parser.add_argument('--n_tasks', type=int, default=1,
            help='Define number of tasks QA model should be trained on. Only necessary, if MTL setting.')
    parser.add_argument('--qa_head', type=str, default='linear',
            help='If linear, put fc linear head on top of BERT; if recurrent, put BiLSTM encoder plus fc linear head on top of BERT.')
    parser.add_argument('--highway_connection', action='store_true',
            help='If provided, put highway connection in between BiLSTM encoder and fc linear output head; NOT relevant for linear head')
    parser.add_argument('--bert_weights', type=str, default='cased',
            help='If cased, load pretrained weights from BERT cased model; if uncased, load pretrained weights from BERT uncased model.')
    parser.add_argument('--batch_size', type=int, default=32,
            help='Define mini-batch size.')
    parser.add_argument('--sd', type=str, default='',
            help='set model save directory for QA model.')
    args = parser.parse_args()
    
    # check whether arg.parser works correctly
    print(args)
    print()
    
    # move model and tensors to GPU, if GPU is available
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    
    # set some crucial hyperparameters
    max_seq_length = 512 # BERT cannot deal with sequences, where T > 512
    doc_stride = 200
    max_query_length = 50
    batch_size = args.batch_size
    
    # create domain_to_idx and dataset_to_idx mappings (necessary for auxiliary tasks)
    domains = ['books', 'electronics', 'grocery', 'movies', 'restaurants', 'tripadvisor', 'all', 'wikipedia']
    datasets = ['SQuAD', 'SubjQA']
    idx_to_domain = dict(enumerate(domains))
    domain_to_idx = {domain: idx for idx, domain in enumerate(domains)}
    idx_to_dataset = dict(enumerate(datasets))
    dataset_to_idx = {dataset: idx for idx, dataset in enumerate(datasets)}
    
     # TODO: figure out, whether we should use pretrained weights from 'bert-base-cased' or 'bert-base-uncased' model
    if args.bert_weights == 'cased':
        
        bert_tokenizer = BertTokenizer.from_pretrained('bert-base-cased')
        pretrained_weights = 'bert-large-cased-whole-word-masking-finetuned-squad'
        
    elif args.bert_weights == 'uncased':
        
        bert_tokenizer == BertTokenizer.from_pretrained('bert-base-uncased')
        pretrained_weights = 'bert-large-uncased-whole-word-masking-finetuned-squad'
        
    else:
        raise ValueError('Pretrained weights must be loaded from an uncased or cased BERT model.')
   
    if args.version == 'train':
        
        if args.finetuning == 'SubjQA' or args.finetuning == 'combined':
        
            subjqa_data_train = get_data(
                                         source='/SubjQA/',
                                         split='/train',
                                         domain='all',
            )

            subjqa_data_dev = get_data(
                                       source='/SubjQA/',
                                       split='/dev',
                                       domain='all',
            )
            
            # convert pd.DataFrames into list of dictionaries (as many dicts as examples)
            subjqa_data_train = convert_df_to_dict(
                                                   subjqa_data_train,
                                                   split='train',
            )
            subjqa_data_dev = convert_df_to_dict(
                                                 subjqa_data_dev,
                                                 split='dev',
            )
            
            # convert dictionaries into instances of preprocessed question-answer-review examples    
            subjqa_examples_train = create_examples(
                                                    subjqa_data_train,
                                                    source='SubjQA',
                                                    is_training=True,
            )

            subjqa_examples_dev = create_examples(
                                                  subjqa_data_dev,
                                                  source='SubjQA',
                                                  is_training=True,
            )
            
            subjqa_features_train = convert_examples_to_features(
                                                                 subjqa_examples_train, 
                                                                 bert_tokenizer,
                                                                 max_seq_length=max_seq_length,
                                                                 doc_stride=doc_stride,
                                                                 max_query_length=max_query_length,
                                                                 is_training=True,
                                                                 domain_to_idx=domain_to_idx,
                                                                 dataset_to_idx=dataset_to_idx,
            )

            subjqa_features_dev = convert_examples_to_features(
                                                               subjqa_examples_dev, 
                                                               bert_tokenizer,
                                                               max_seq_length=max_seq_length,
                                                               doc_stride=doc_stride,
                                                               max_query_length=max_query_length,
                                                               is_training=True,
                                                               domain_to_idx=domain_to_idx,
                                                               dataset_to_idx=dataset_to_idx,
            )
            
            subjqa_tensor_dataset_train = create_tensor_dataset(
                                                                subjqa_features_train,
                                                                evaluate=False,
            )

            subjqa_tensor_dataset_dev = create_tensor_dataset(
                                                              subjqa_features_dev,
                                                              evaluate=False,
            )
                
        elif args.finetuning == 'SQuAD' or args.finetuning == 'combined':
            
            squad_data_train = get_data(
                                        source='/SQuAD/',
                                        split='train',
            )
            
            squad_examples_train = create_examples(
                                       squad_data_train,
                                       source='SQuAD',
                                       is_training=True,
            )

            # create train and dev examples from SQuAD train set only
            squad_examples_train, squad_examples_dev = split_into_train_and_dev(squad_examples_train)
            
            
            squad_features_train = convert_examples_to_features(
                                                                squad_examples_train, 
                                                                bert_tokenizer,
                                                                max_seq_length=max_seq_length,
                                                                doc_stride=doc_stride
                                                                max_query_length=max_query_length,
                                                                is_training=True,
                                                                domain_to_idx=domain_to_idx,
                                                                dataset_to_idx=dataset_to_idx,
            )

            squad_features_dev = convert_examples_to_features(
                                                             squad_examples_dev, 
                                                             bert_tokenizer,
                                                             max_seq_length=max_seq_length,
                                                             doc_stride=doc_stride
                                                             max_query_length=max_query_length,
                                                             is_training=True,
                                                             domain_to_idx=domain_to_idx,
                                                             dataset_to_idx=dataset_to_idx,
            )
            
            squad_tensor_dataset_train = create_tensor_dataset(
                                                   squad_features_train,
                                                   evaluate=False,
            )

            squad_tensor_dataset_dev = create_tensor_dataset(
                                                 squad_features_dev,
                                                 evaluate=False,
            )
        
        if args.finetuning == 'SQuAD':
            
            train_dl = create_batches(
                                      dataset=squad_tensor_dataset_train,
                                      batch_size=batch_size,
                                      split='train',
            )

            val_dl = create_batches(
                                    dataset=squad_tensor_dataset_dev,
                                    batch_size=batch_size,
                                    split='eval',
            )
            
        elif args.finetuning == 'SubjQA':
            
            train_dl = create_batches(
                                      dataset=subjqa_tensor_dataset_train,
                                      batch_size=batch_size,
                                      split='train',
            )

            val_dl = create_batches(
                                    dataset=subjqa_tensor_dataset_dev,
                                    batch_size=batch_size,
                                    split='eval',
            )
                
        elif args.finetuning == 'combined':
            
            train_dl = AlternatingBatchGenerator(
                                                 squad_tensor_dataset_train,
                                                 subjqa_tensor_dataset_train,
                                                 batch_size=batch_size,
                                                 split='train',
            )

            val_dl = AlternatingBatchGenerator(
                                               squad_tensor_dataset_train,
                                               subjqa_tensor_dataset_train,
                                               batch_size=batch_size,
                                               split='eval',
            )
                
        
    # we always test on SubjQA
    elif args.version == 'test':
            
            subjqa_data_test = convert_df_to_dict(
                                                  subjqa_data_test,
                                                  split='test',
            )
            
            # convert dictionaries into instances of preprocessed question-answer-review examples    
            subjqa_examples_test = create_examples(
                                                   subjqa_data_test,
                                                   source='SubjQA',
                                                   is_training=True,
            )
            
            subjqa_features_test = convert_examples_to_features(
                                                                subjqa_examples_test, 
                                                                bert_tokenizer,
                                                                max_seq_length=max_seq_length,
                                                                doc_stride=doc_stride,
                                                                max_query_length=max_query_length,
                                                                is_training=True,
                                                                domain_to_idx=domain_to_idx,
                                                                dataset_to_idx=dataset_to_idx,
            )
            
            subjqa_tensor_dataset_test = create_tensor_dataset(
                                                               subjqa_features_test,
                                                               evaluate=False,
            )  
            
            test_dl = create_batches(
                                     dataset=subjqa_tensor_dataset_test,
                                     batch_size=batch_size,
                                     split='eval',
            )
            
            
    # initialise QA model
    qa_head_name = 'RecurrentQAHead' if args.qa_head == 'recurrent' else 'LinearQAHead'
    model = BertForQA.from_pretrained(
                                      pretrained_weights,
                                      qa_head_name=qa_head_name,
                                      max_seq_length=max_seq_length,
                                      highway_connection=args.highway_connection,
                                      multitask=args.multitask,
    )
    
    # set model to device
    model.to(device)