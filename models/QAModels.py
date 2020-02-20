__all__ = ['DistilBertForQA']

import numpy as np
import torch.nn as nn

import random
import torch

from transformers import DistilBertModel, DistilBertPreTrainedModel
from models.modules.QAHeads import *

# set random seeds to reproduce results
np.random.seed(42)
random.seed(42)
torch.manual_seed(42)

# set device
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

class DistilBertForQA(DistilBertPreTrainedModel):
    
    def __init__(
                 self,
                 config,
                 max_seq_length:int=512,
                 encoder:bool=False,
                 highway_connection:bool=False,
                 decoder:bool=False,
                 multitask:bool=False,
                 adversarial:bool=False,
                 n_aux_tasks=None,
                 n_domain_labels=None,
    ):        
        super(DistilBertForQA, self).__init__(config)
        self.distilbert = DistilBertModel(config)
        self.max_seq_length = max_seq_length
        self.encoder = encoder
        self.highway_connection = highway_connection
        self.decoder = decoder
        self.multitask = multitask
        self.adversarial = adversarial
        self.n_aux_tasks = n_aux_tasks
        self.n_domain_labels = n_domain_labels
        
        if self.multitask: assert isinstance(self.n_aux_tasks, int), "If MTL setting, number of auxiliary tasks must be defined"
        
        if self.encoder:
            self.qa_head = RecurrentQAHead(
                                           in_size=config.dim,
                                           n_labels_qa=config.num_labels,
                                           qa_dropout_p=config.qa_dropout,
                                           max_seq_length=self.max_seq_length,
                                           highway_block=self.highway_connection,
                                           multitask=self.multitask,
                                           decoder=self.decoder,
                                           n_aux_tasks=self.n_aux_tasks,
                                           n_domain_labels=self.n_domain_labels,
                                           adversarial=self.adversarial,
                                           )
        else:
            self.qa_head = LinearQAHead(
                                        in_size=config.dim,
                                        n_labels_qa=config.num_labels,
                                        qa_dropout_p=config.qa_dropout,
                                        highway_block=self.highway_connection,
                                        multitask=self.multitask,
                                        n_aux_tasks=self.n_aux_tasks,
                                        n_domain_labels=self.n_domain_labels,
                                        adversarial=self.adversarial,
                                        )
        self.init_weights()

    def forward(
                self,
                input_ids:torch.Tensor,
                attention_masks:torch.Tensor,
                token_type_ids:torch.Tensor,
                task:str,
                position_ids=None,
                head_mask=None,
                inputs_embeds=None,
                input_lengths=None,
                start_positions=None,
                end_positions=None,
    ):
        # NOTE: token_type_ids == segment_ids
        distilbert_output = self.distilbert(
                                        input_ids=input_ids,
                                        #token_type_ids=token_type_ids,
                                        attention_mask=attention_masks,
                                        head_mask=head_mask,
                                        )
      
        if self.encoder:
            return self.qa_head(
                                distilbert_output=distilbert_output,
                                seq_lengths=input_lengths,
                                task=task,
                                start_positions=start_positions,
                                end_positions=end_positions,
            )
        else:
            return self.qa_head(
                                distilbert_output=distilbert_output,
                                task=task,
                                start_positions=start_positions,
                                end_positions=end_positions,
            )