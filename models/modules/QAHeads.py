__all__ = [
           'LinearQAHead',
           'RecurrentQAHead',
]

import random
import torch
import torch.nn as nn
import torch.nn.functional as F

from models.modules.RNNs import *
from models.modules.GradReverse import *
from models.modules.Highway import Highway
from models.utils import to_cpu

# set device
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
       
class LinearQAHead(nn.Module):
    
    def __init__(
                 self,
                 in_size:int,
                 n_labels_qa:int,
                 qa_dropout_p:float,
                 highway_block:bool=False,
                 multitask:bool=False,
                 n_aux_tasks=None,
                 aux_dropout_p:float=0.1,
                 n_domain_labels=None,
                 n_qa_type_labels=None,
                 adversarial:bool=False,
                 dataset_agnostic:bool=False,
                 review_agnostic:bool=False,
                 task:str='QA',
    ):
        
        super(LinearQAHead, self).__init__()
        self.in_size = in_size
        self.n_labels = n_labels_qa
        assert self.n_labels == 2
        self.multitask = multitask
        self.n_aux_tasks = n_aux_tasks
        self.aux_dropout_p = aux_dropout_p
        self.qa_dropout_p = qa_dropout_p
        self.task = task
        self.n_qa_type_labels = n_qa_type_labels
        self.dataset_agnostic = dataset_agnostic
        self.review_agnostic = review_agnostic

        if highway_block:
            self.highway_1 = Highway(self.in_size)
            self.highway_2 = Highway(self.in_size)

        # fully-connected QA output layer with dropout probability of 0.1
        self.fc_qa = nn.Linear(self.in_size, self.n_labels)
        nn.init.xavier_uniform_(self.fc_qa.weight)
        self.qa_dropout = nn.Dropout(p = self.qa_dropout_p)

        if (self.task == 'QA' and self.multitask) or self.task == 'Sbj_Classification' or self.task == 'all':
            
            # define dropout layer for auxiliary classification tasks
            self.aux_dropout = nn.Dropout(p = self.aux_dropout_p)
            
            # fully-connected subjectivity output layers (present in all MTL settings)
            self.fc_sbj_1 = nn.Linear(self.in_size, self.in_size)
            self.fc_sbj_2 = nn.Linear(self.in_size, self.in_size)

            if isinstance(self.n_qa_type_labels, int):
                # multi-way qa_type classification task
                self.fc_sbj_q = nn.Linear(self.in_size, n_qa_type_labels) # fc sbj. layer for questions
                sbj_layers = [self.fc_sbj_1, self.fc_sbj_2, self.fc_sbj_q]
            else:
                # binary qa_type classification task
                self.fc_sbj_a = nn.Linear(self.in_size, 1) # fc sbj. layer for answers
                self.fc_sbj_q = nn.Linear(self.in_size, 1) # fc sbj. layer for questions
                sbj_layers = [self.fc_sbj_1, self.fc_sbj_2, self.fc_sbj_a, self.fc_sbj_q]

            for fc_sbj in sbj_layers:
                nn.init.xavier_uniform_(fc_sbj.weight)

        
        elif self.task == 'Domain_Classification':

            # define dropout layer for auxiliary classification tasks
            self.aux_dropout = nn.Dropout(p = self.aux_dropout_p)

            assert isinstance(n_domain_labels, int), 'If model is to perform domain classification, domain labels must be provided'
            self.n_domain_labels = n_domain_labels

            # fully-connected review domain output layers (second auxiliary task)
            self.fc_domain_1 = nn.Linear(self.in_size, self.in_size)
            self.fc_domain_2 = nn.Linear(self.in_size, self.in_size)
            self.fc_domain_3 = nn.Linear(self.in_size, self.n_domain_labels)

            for fc_domain in [self.fc_domain_1, self.fc_domain_2, self.fc_domain_3]:
                nn.init.xavier_uniform_(fc_domain.weight)
        
        if self.task == 'QA' and self.multitask:

            # define, whether we want to perform adversarial training with a GRL between BERT and classifiers
            self.adversarial = adversarial
            
            if self.n_aux_tasks == 2 and self.dataset_agnostic:

                # fully-connected dataset output layers (second auxiliary task)
                self.fc_ds_1 = nn.Linear(self.in_size, self.in_size)
                self.fc_ds_2 = nn.Linear(self.in_size, self.in_size)
                self.fc_ds_3 = nn.Linear(self.in_size, 1)

                for fc_ds in [self.fc_ds_1, self.fc_ds_2, self.fc_ds_3]:
                    nn.init.xavier_uniform_(fc_ds.weight)

            elif self.n_aux_tasks == 2 and not self.dataset_agnostic:
                assert isinstance(n_domain_labels, int), 'Total number of domain labels must be provided'
                self.n_domain_labels = n_domain_labels

                # fully-connected review domain output layers (second auxiliary task)
                self.fc_domain_1 = nn.Linear(self.in_size, self.in_size)
                self.fc_domain_2 = nn.Linear(self.in_size, self.in_size)
                self.fc_domain_3 = nn.Linear(self.in_size, self.n_domain_labels)

                for fc_domain in [self.fc_domain_1, self.fc_domain_2, self.fc_domain_3]:
                    nn.init.xavier_uniform_(fc_domain.weight)

            elif self.n_aux_tasks > 2:
                raise ValueError("Model cannot perform more than 2 auxiliary tasks along main task.")

        elif self.task == 'all':
            assert isinstance(n_domain_labels, int), 'If model is to perform sequential transfer, total number of domain labels must be provided'
            self.n_domain_labels = n_domain_labels

            # fully-connected review domain output layers (second auxiliary task)
            self.fc_domain_1 = nn.Linear(self.in_size, self.in_size)
            self.fc_domain_2 = nn.Linear(self.in_size, self.in_size)
            self.fc_domain_3 = nn.Linear(self.in_size, self.n_domain_labels)

            for fc_domain in [self.fc_domain_1, self.fc_domain_2, self.fc_domain_3]:
                nn.init.xavier_uniform_(fc_domain.weight)
                    
    def forward(
                self,
                distilbert_output:torch.Tensor,
                task:str,
                aux_targets=None,
                start_positions=None,
                end_positions=None,
                input_lengths=None,
                output_last_hiddens_cls:bool=False,
                output_all_hiddens_cls:bool=False,
                output_all_hiddens:bool=False,
                output_last_hiddens:bool=False,
    ):
        sequence_output = distilbert_output[0] # last hidden-state is the first element of the output tuple
        sequence_output = self.qa_dropout(sequence_output)
        
        if hasattr(self, 'highway'):
            # pass BERT representations through Highway block (to enhance information flow)
            sequence_output = self.highway_1(sequence_output) 
            sequence_output = self.highway_2(sequence_output)

        if task == 'QA':

            if isinstance(aux_targets, torch.Tensor):
                
                def concat_embeds_logits(seq_out:torch.Tensor, aux_targets:torch.Tensor):
                    seqs_cat_logits = []
                    assert seq_out.size(0) == aux_targets.size(0)
                    for i, seq in enumerate(seq_out):
                        seq_cat_logits = [torch.cat((seq[t], aux_targets[i])).detach().cpu().numpy().tolist() for t, _ in enumerate(seq)]
                        seqs_cat_logits.append(seq_cat_logits)
                    return torch.tensor(seqs_cat_logits, requires_grad=True).to(device)

                sequence_output = concat_embeds_logits(sequence_output, aux_targets) 

            logits = self.fc_qa(sequence_output)
            start_logits, end_logits = logits.split(1, dim=-1)
            start_logits = start_logits.squeeze(-1)
            end_logits = end_logits.squeeze(-1)

            outputs = (start_logits, end_logits,) # + distilbert_output[1:]

            if output_last_hiddens_cls:
                sequence_output = sequence_output[:, 0, :].squeeze(1)
                return outputs, sequence_output

            if output_last_hiddens:
                bert_hidden_states = distilbert_output[1] # tuple of all hidden states (output of embeddings + output for each transformer layer)
                #bert_hidden_states = bert_hidden_states[-1] # extract hidden states from last transformer layer only
                bert_hidden_states = bert_hidden_states[-3:-1] # for now, extract hidden states from third-to-the-last layer to compute cosine loss
                return outputs, bert_hidden_states

            if output_all_hiddens_cls:
                bert_hidden_states = distilbert_output[1] # tuple of all hidden states (output of embeddings + output for each transformer layer)
                bert_hidden_states = bert_hidden_states[1:] # extract hidden states from each of the 6 transformer layers
                bert_hidden_states_cls = tuple(hidden[:, 0, :].squeeze(1) for hidden in bert_hidden_states) 
                return outputs, bert_hidden_states_cls

            if output_all_hiddens:
                assert isinstance(input_lengths, torch.Tensor)
                bert_hidden_states = distilbert_output[1] # tuple of all hidden states (output of embeddings + output for each transformer layer)
                bert_hidden_states = bert_hidden_states[1:] # extract hidden states from all transformer layers

                """
                def remove_pad_token_hiddens(input_lengths:torch.Tensor, hidden_states:tuple):
                    n_layers = len(hidden_states)
                    return tuple([to_cpu(hidden_states[l], detach=True, to_numpy=True)[i, :seq_len, :].tolist() for i, seq_len in enumerate(input_lengths)] for l in range(n_layers))
                bert_hidden_states = remove_pad_token_hiddens(input_lengths, bert_hidden_states)
                """
                
                return outputs, bert_hidden_states

            return outputs

        else:
            if task == 'Sbj_Class':
                if hasattr(self, 'adversarial'):
                    if self.dataset_agnostic or self.review_agnostic:
                        sequence_output = sequence_output[:, 0, :]
                    else:
                        # reverse gradients to learn qa-type invariant features (i.e., semi-supervised domain-adaptation)
                        sequence_output = grad_reverse(sequence_output)
                        sequence_output = sequence_output[:, 0, :]
                else:
                    sequence_output = sequence_output[:, 0, :]
                """
                #######################################
                ### version without skip connection ###
                #######################################

                sbj_out = self.fc_sbj_1(sequence_output)
                sbj_out = self.aux_dropout(sbj_out)
                #sbj_out = self.fc_sbj_2(sbj_out)
                #sbj_out = self.aux_dropout(sbj_out)
                sbj_logits_a = self.fc_sbj_a(sbj_out)
                sbj_logits_q = self.fc_sbj_q(sbj_out)

                """
                # introduce skip connection (add output of previous layer to linear transform block) to encode more information
                sbj_out = sequence_output + self.fc_sbj_2(F.relu(self.aux_dropout(self.fc_sbj_1(sequence_output))))
                sbj_out = self.aux_dropout(sbj_out)

                if isinstance(self.n_qa_type_labels, int):
                    sbj_logits_q = self.fc_sbj_q(sbj_out)
                    sbj_logits_q = sbj_logits_q.squeeze(-1)

                    if output_last_hiddens_cls:
                        sequence_output = sequence_output.squeeze(1)
                        return sbj_logits_q, sequence_output
                    
                    if output_all_hiddens_cls:
                        bert_hidden_states = distilbert_output[1] # tuple of all hidden states (output of embeddings + output for each transformer layer)
                        bert_hidden_states = bert_hidden_states[1:] # extract hidden states from each of the 6 transformer layers
                        bert_hidden_states_cls = tuple(hidden[:, 0, :].squeeze(1) for hidden in bert_hidden_states) 
                        return sbj_logits_q, bert_hidden_states_cls

                    return sbj_logits_q
                else:
                    sbj_logits_a = self.fc_sbj_a(sbj_out)
                    sbj_logits_q = self.fc_sbj_q(sbj_out)

                    sbj_logits_a = sbj_logits_a.squeeze(-1) # remove 2nd dimension: [batch_size, 1] ==> [batch_size]
                    sbj_logits_q = sbj_logits_q.squeeze(-1) # remove 2nd dimension: [batch_size, 1] ==> [batch_size]

                    return sbj_logits_a, sbj_logits_q

            elif task == 'Domain_Class':
                if hasattr(self, 'adversarial'):
                    # reverse gradients to learn review-domain invariant features (i.e., semi-supervised domain-adaptation)
                    sequence_output = grad_reverse(sequence_output)
                    sequence_output = sequence_output[:, 0, :]
                else:
                    sequence_output = sequence_output[:, 0, :]

                """
                #######################################
                ### version without skip connection ###
                #######################################

                domain_out = self.fc_domain_1(sequence_output)
                domain_out = self.aux_dropout(domain_out)
                domain_out = self.fc_domain_2(domain_out)
                domain_out = self.aux_dropout(domain_out)
                domain_logits = self.fc_domain_3(domain_out)

                """

                # introduce skip connection (add output of previous layer to linear transform block) to encode more information
                domain_out = sequence_output + self.fc_domain_2(F.relu(self.aux_dropout(self.fc_domain_1(sequence_output))))
                domain_out = self.aux_dropout(domain_out)
                domain_logits = self.fc_domain_3(domain_out)
                domain_logits = domain_logits.squeeze(-1) # remove 2nd dimension -> shape: [batch_size, 1] ==> shape: [batch_size]
                
                if output_last_hiddens_cls:
                    sequence_output = sequence_output.squeeze(1)
                    return domain_logits, sequence_output

                if output_all_hiddens_cls:
                    bert_hidden_states = distilbert_output[1] # tuple of all hidden states (output of embeddings + output for each transformer layer)
                    bert_hidden_states = bert_hidden_states[1:] # extract hidden states from each of the 6 transformer layers
                    bert_hidden_states_cls = tuple(hidden[:, 0, :].squeeze(1) for hidden in bert_hidden_states) 
                    return domain_logits, bert_hidden_states_cls

                return domain_logits

            elif task == 'Dataset_Class':
                # reverse gradients to learn dataset agnostic (i.e., domain-invariant) features
                assert hasattr(self, 'adversarial') and self.dataset_agnostic, 'Dataset classification task must be defined as an adversarial task'
                sequence_output = grad_reverse(sequence_output)
                sequence_output = sequence_output[:, 0, :]

                """
                #######################################
                ### version without skip connection ###
                #######################################

                ds_out = self.fc_ds_1(sequence_output)
                ds_out = self.aux_dropout(ds_out)
                ds_out = self.fc_ds_2(ds_out)
                ds_out = self.aux_dropout(ds_out)
                ds_logits = self.fc_ds_3(ds_out)

                """

                # introduce skip connection (add output of previous layer to linear transform block) to encode more information
                ds_out = sequence_output + self.fc_ds_2(F.relu(self.aux_dropout(self.fc_ds_1(sequence_output))))
                ds_out = self.aux_dropout(ds_out)
                ds_logits = self.fc_ds_3(ds_out)
                ds_logits = ds_logits.squeeze(-1) # remove 2nd dimension -> shape: [batch_size, 1] ==> shape: [batch_size]
                return ds_logits
        
class RecurrentQAHead(nn.Module):
    
    def __init__(
                 self,
                 in_size:int,
                 n_labels_qa:int,
                 qa_dropout_p:float,
                 max_seq_length:int,
                 highway_block:bool=False,
                 multitask:bool=False,
                 n_aux_tasks=None,
                 aux_dropout_p:float=0.1,
                 n_domain_labels=None,
                 n_qa_type_labels=None,
                 adversarial:bool=False,
                 dataset_agnostic:bool=False,
                 review_agnostic:bool=False,
                 task:str='QA',
    ):
        super(RecurrentQAHead, self).__init__()
        
        self.in_size = in_size
        self.n_labels = n_labels_qa
        assert self.n_labels == 2
        self.qa_dropout_p = qa_dropout_p
        self.multitask = multitask
        self.n_aux_tasks = n_aux_tasks
        self.aux_dropout_p = aux_dropout_p
        self.n_recurrent_layers = 2 # set number of recurrent layers to 1 or 2 (more are not necessary and computationally inefficient)
        self.rnn_version = 'LSTM' # must be one of {"LSTM", "GRU"}
        self.task = task
        self.n_qa_type_labels = n_qa_type_labels
        self.dataset_agnostic = dataset_agnostic
        self.review_agnostic = review_agnostic


        self.rnn_encoder = BiLSTM(max_seq_length, in_size=self.in_size, n_layers=self.n_recurrent_layers) if self.rnn_version == 'LSTM' else BiGRU(max_seq_length, in_size=self.in_size, n_layers=self.n_recurrent_layers)
        
        if highway_block:
            self.highway_1 = Highway(self.in_size) # Highway block in-between BERT and BiLSTMs
            self.highway_2 = Highway(self.in_size) # Highway block in-between BiLSTMs and fully-connected (task-specific) output layers
            
        # fully-connected QA output layer with dropout probability of 0.1
        self.fc_qa = nn.Linear(self.in_size, self.n_labels)
        nn.init.xavier_uniform_(self.fc_qa.weight)
        self.qa_dropout = nn.Dropout(p = self.qa_dropout_p)

        if (self.task == 'QA' and self.multitask) or self.task == 'Sbj_Classification' or self.task == 'all':
            # define dropout layer for auxiliary classification tasks
            self.aux_dropout = nn.Dropout(p = self.aux_dropout_p)
            
            # fully-connected subjectivity output layers (present in every MTL setting)
            self.fc_sbj_1 = nn.Linear(self.in_size, self.in_size)
            self.fc_sbj_2 = nn.Linear(self.in_size, self.in_size)

            if isinstance(self.n_qa_type_labels, int):
                # multi-way qa_type classification task
                self.fc_sbj_q = nn.Linear(self.in_size, n_qa_type_labels) # fc sbj. layer for questions
                sbj_layers = [self.fc_sbj_1, self.fc_sbj_2, self.fc_sbj_q]
            else:
                # binary qa_type classification task
                self.fc_sbj_a = nn.Linear(self.in_size, 1) # fc sbj. layer for answers
                self.fc_sbj_q = nn.Linear(self.in_size, 1) # fc sbj. layer for questions
                sbj_layers = [self.fc_sbj_1, self.fc_sbj_2, self.fc_sbj_a, self.fc_sbj_q]

            for fc_sbj in sbj_layers:
                nn.init.xavier_uniform_(fc_sbj.weight)
   
        elif self.task == 'Domain_Classification':

            # define dropout layer for auxiliary classification tasks
            self.aux_dropout = nn.Dropout(p = self.aux_dropout_p)

            assert isinstance(n_domain_labels, int), 'If model is to perform domain classification, domain labels must be provided'
            self.n_domain_labels = n_domain_labels

            # fully-connected review domain output layers (second auxiliary task)
            self.fc_domain_1 = nn.Linear(self.in_size, self.in_size)
            self.fc_domain_2 = nn.Linear(self.in_size, self.in_size)
            self.fc_domain_3 = nn.Linear(self.in_size, self.n_domain_labels)

            for fc_domain in [self.fc_domain_1, self.fc_domain_2, self.fc_domain_3]:
                nn.init.xavier_uniform_(fc_domain.weight)
        
        if self.task == 'QA' and self.multitask:

            # define, whether we want to perform adversarial training with a GRL between feature extractor and classifiers
            self.adversarial = adversarial
            
            if self.n_aux_tasks == 2 and self.dataset_agnostic:

                # fully-connected dataset output layers (second auxiliary task)
                self.fc_ds_1 = nn.Linear(self.in_size, self.in_size)
                self.fc_ds_2 = nn.Linear(self.in_size, self.in_size)
                self.fc_ds_3 = nn.Linear(self.in_size, 1)

                for fc_ds in [self.fc_ds_1, self.fc_ds_2, self.fc_ds_3]:
                    nn.init.xavier_uniform_(fc_ds.weight)

            elif self.n_aux_tasks == 2 and not self.dataset_agnostic:
                assert isinstance(n_domain_labels, int), 'Total number of domain labels must be provided'
                self.n_domain_labels = n_domain_labels

                # fully-connected review domain output layers (second auxiliary task)
                self.fc_domain_1 = nn.Linear(self.in_size, self.in_size)
                self.fc_domain_2 = nn.Linear(self.in_size, self.in_size)
                self.fc_domain_3 = nn.Linear(self.in_size, self.n_domain_labels)

                for fc_domain in [self.fc_domain_1, self.fc_domain_2, self.fc_domain_3]:
                    nn.init.xavier_uniform_(fc_domain.weight)

            elif self.n_aux_tasks > 2:
                raise ValueError("Model cannot perform more than 2 auxiliary tasks.")

        elif self.task == 'all':
            assert isinstance(n_domain_labels, int), 'If model is to perform sequential transfer, total number of domain labels must be provided'
            self.n_domain_labels = n_domain_labels

            # fully-connected review domain output layers (second auxiliary task)
            self.fc_domain_1 = nn.Linear(self.in_size, self.in_size)
            self.fc_domain_2 = nn.Linear(self.in_size, self.in_size)
            self.fc_domain_3 = nn.Linear(self.in_size, self.n_domain_labels)

            for fc_domain in [self.fc_domain_1, self.fc_domain_2, self.fc_domain_3]:
                nn.init.xavier_uniform_(fc_domain.weight)

    def forward(
                self,
                distilbert_output:torch.Tensor,
                seq_lengths:torch.Tensor,
                task:str,
                aux_targets=None,
                start_positions=None,
                end_positions=None,
                output_last_hiddens_cls:bool=False,
                output_all_hiddens_cls:bool=False,
                output_all_hiddens:bool=False,
                output_last_hiddens:bool=False,
    ):
        sequence_output = distilbert_output[0] # last hidden-state is the first element of the output tuple
        sequence_output = self.qa_dropout(sequence_output)

        if hasattr(self, 'highway'):
            # pass BERT representations through Highway block (to enhance information flow)
            sequence_output = self.highway_1(sequence_output)

        hidden_rnn = self.rnn_encoder.init_hidden(sequence_output.shape[0])
        
        # pass BERT representations through BiLSTM or (BiGRU) to compute both temporal dependencies and global interactions among feature representations
        sequence_output, hidden_rnn = self.rnn_encoder(sequence_output, seq_lengths, hidden_rnn)
        
        if hasattr(self, 'highway'):
            sequence_output = self.highway_2(sequence_output)
        
        if task == 'QA':

            if isinstance(aux_targets, torch.Tensor):
                
                def concat_embeds_logits(seq_out:torch.Tensor, aux_targets:torch.Tensor):
                    seqs_cat_logits = []
                    assert seq_out.size(0) == aux_targets.size(0)
                    for i, seq in enumerate(seq_out):
                        seq_cat_logits = [torch.cat((seq[t], aux_targets[i])).detach().cpu().numpy().tolist() for t, _ in enumerate(seq)]
                        seqs_cat_logits.append(seq_cat_logits)
                    return torch.tensor(seqs_cat_logits, requires_grad=True).to(device)

                sequence_output = concat_embeds_logits(sequence_output, aux_targets) 

            logits = self.fc_qa(sequence_output)
            start_logits, end_logits = logits.split(1, dim=-1)
            start_logits = start_logits.squeeze(-1)
            end_logits = end_logits.squeeze(-1)

            outputs = (start_logits, end_logits,)

            if output_last_hiddens_cls:
                bert_hidden_states = distilbert_output[1] # tuple of all hidden states (output of embeddings + output for each transformer layer)
                bert_hidden_states = bert_hidden_states[-1]
                sequence_output = bert_hidden_states[:, 0, :].squeeze(1)
                return outputs, sequence_output

            if output_last_hiddens:
                bert_hidden_states = distilbert_output[1] # tuple of all hidden states (output of embeddings + output for each transformer layer)
                bert_hidden_states = bert_hidden_states[-1] # extract hidden states from last transformer layer only
                return outputs, bert_hidden_states

            if output_all_hiddens_cls:
                bert_hidden_states = distilbert_output[1] # tuple of all hidden states (output of embeddings + output for each transformer layer)
                bert_hidden_states = bert_hidden_states[1:] # extract hidden states from each of the 6 transformer layers
                bert_hidden_states_cls = tuple(hidden[:, 0, :].squeeze(1) for hidden in bert_hidden_states) 
                return outputs, bert_hidden_states_cls

            if output_all_hiddens:
                assert isinstance(input_lengths, torch.Tensor)
                bert_hidden_states = distilbert_output[1] # tuple of all hidden states (output of embeddings + output for each transformer layer)
                bert_hidden_states = bert_hidden_states[1:] # extract hidden states from all transformer layers

                """
                def remove_pad_token_hiddens(input_lengths:torch.Tensor, hidden_states:tuple):
                    n_layers = len(hidden_states)
                    return tuple([to_cpu(hidden_states[l], detach=True, to_numpy=True)[i, :seq_len, :].tolist() for i, seq_len in enumerate(input_lengths)] for l in range(n_layers))
                bert_hidden_states = remove_pad_token_hiddens(input_lengths, bert_hidden_states)
                """
                
                return outputs, bert_hidden_states

            return outputs

        else:
            if task == 'Sbj_Class':
                if hasattr(self, 'adversarial'):
                    if self.dataset_agnostic or self.review_agnostic:
                        sequence_output = sequence_output[:, -1, :]
                    else:
                         # reverse gradients to learn qa-type invariant features
                         sequence_output = grad_reverse(sequence_output)
                         sequence_output = sequence_output[:, -1, :]
                else:
                    sequence_output = sequence_output[:, -1, :]

                """
                #######################################
                ### version without skip connection ###
                #######################################

                sbj_out = self.fc_sbj_1(sequence_output)
                sbj_out = self.aux_dropout(sbj_out)
                sbj_out = self.fc_sbj_2(sbj_out)
                sbj_out = self.aux_dropout(sbj_out)
                sbj_logits_a = self.fc_sbj_a(sbj_out)
                sbj_logits_q = self.fc_sbj_q(sbj_out)

                """

                # introduce skip connection (add output of previous layer to linear transform block) to encode more information
                sbj_out = sequence_output + self.fc_sbj_2(F.relu(self.aux_dropout(self.fc_sbj_1(sequence_output))))
                sbj_out = self.aux_dropout(sbj_out)

                if isinstance(self.n_qa_type_labels, int):
                    sbj_logits_q = self.fc_sbj_q(sbj_out)
                    sbj_logits_q = sbj_logits_q.squeeze(-1)
                    return sbj_logits_q
                else:
                    sbj_logits_a = self.fc_sbj_a(sbj_out)
                    sbj_logits_q = self.fc_sbj_q(sbj_out)
                    sbj_logits_a = sbj_logits_a.squeeze(-1) # remove 2nd dimension - shape: [batch_size, 1] ==> shape: [batch_size]
                    sbj_logits_q = sbj_logits_q.squeeze(-1) # remove 2nd dimension - shape: [batch_size, 1] ==> shape: [batch_size]
                    return sbj_logits_a, sbj_logits_q

            elif task == 'Domain_Class':
                if hasattr(self, 'adversarial'):
                    # reverse gradients to learn review-domain invariant features
                    sequence_output = grad_reverse(sequence_output)
                    sequence_output = sequence_output[:, -1, :]
                else:
                    sequence_output = sequence_output[:, -1, :]

                """
                #######################################
                ### version without skip connection ###
                #######################################

                domain_out = self.fc_domain_1(sequence_output)
                domain_out = self.aux_dropout(domain_out)
                domain_out = self.fc_domain_2(domain_out)
                domain_out = self.aux_dropout(domain_out)
                domain_logits = self.fc_domain_3(domain_out)

                """

                # introduce skip connection (add output of previous layer to linear transform block) to encode more information
                domain_out = sequence_output + self.fc_domain_2(F.relu(self.aux_dropout(self.fc_domain_1(sequence_output))))
                domain_out = self.aux_dropout(domain_out)
                domain_logits = self.fc_domain_3(domain_out)
                domain_logits = domain_logits.squeeze(-1) # remove 2nd dimension - shape: [batch_size, 1] ==> shape: [batch_size]
                return domain_logits

            elif task == 'Dataset_Class':
                # reverse gradients to learn dataset agnostic (i.e., domain invariant) features
                assert hasattr(self, 'adversarial') and self.dataset_agnostic, 'Dataset classification task must be defined as an adversarial task'
                sequence_output = grad_reverse(sequence_output)
                sequence_output = sequence_output[:, -1, :]
                
                """
                #######################################
                ### version without skip connection ###
                #######################################

                ds_out = self.fc_ds_1(sequence_output)
                ds_out = self.aux_dropout(ds_out)
                ds_out = self.fc_ds_2(ds_out)
                ds_out = self.aux_dropout(ds_out)
                ds_logits = self.fc_ds_3(ds_out)

                """

                # introduce skip connection (add output of previous layer to linear transform block) to encode more information
                ds_out = sequence_output + self.fc_ds_2(F.relu(self.aux_dropout(self.fc_ds_1(sequence_output))))
                ds_out = self.aux_dropout(ds_out)
                ds_logits = self.fc_ds_3(ds_out)
                ds_logits = ds_logits.squeeze(-1) # remove 2nd dimension - shape: [batch_size, 1] ==> shape: [batch_size]
                return ds_logits