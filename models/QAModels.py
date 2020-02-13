__all__ = ['BertForQA']

import numpy as np
import torch.nn as nn

import random
import torch

from transformers import BertModel, BertPreTrainedModel

from models.modules.Encoder import *
from models.modules.Highway import Highway
from models.modules.QAHeads import *

# set random seeds to reproduce results
np.random.seed(42)
random.seed(42)
torch.manual_seed(42)

# set device
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

"""
is_cuda = torch.cuda.is_available()

if is_cuda:
    device = torch.device("cuda")
    print("GPU is available")
else:
    device = torch.device("cpu")
    print("GPU not available, CPU used")
"""

class BertForQA(BertPreTrainedModel):
    
    def __init__(
                 self,
                 config,
                 qa_head_name:str,
                 max_seq_length:int=512,
                 highway_connection:bool=False,
                 multitask:bool=False,
                 adversarial:bool=False,
                 n_aux_tasks=None,
                 n_domain_labels=None,
    ):
        
        super(BertForQA, self).__init__(config)
        self.bert = BertModel(config)
        self.qa_head_name = qa_head_name
        self.max_seq_length = max_seq_length
        self.highway_connection = highway_connection
        self.multitask = multitask
        self.adversarial = adversarial
        self.n_aux_tasks = n_aux_tasks
        self.n_domain_labels = n_domain_labels
        
        assert isinstance(self.qa_head_name, str), "QA head must be defined, and has to be one of {'LinearQAHead', 'RecurrentQAHead'}"
        if self.multitask: assert isinstance(self.n_aux_tasks, int), "If MTL setting, number of auxiliary tasks must be defined"

        
        if self.qa_head_name == 'LinearQAHead':
            self.qa_head = LinearQAHead(
                                        in_size=config.hidden_size,
                                        n_labels_qa=config.num_labels,
                                        highway_block=self.highway_connection,
                                        multitask=self.multitask,
                                        n_aux_tasks=self.n_aux_tasks,
                                        n_domain_labels=self.n_domain_labels,
                                        adversarial=self.adversarial,
            )
            
        elif self.qa_head_name == 'RecurrentQAHead':
            self.qa_head = RecurrentQAHead(
                                           max_seq_length=self.max_seq_length,
                                           in_size=config.hidden_size,
                                           n_labels_qa=config.num_labels,
                                           highway_block=self.highway_connection,
                                           multitask=self.multitask,
                                           n_aux_tasks=self.n_aux_tasks,
                                           n_domain_labels=self.n_domain_labels,
                                           adversarial=self.adversarial,
            )
            
    def forward(
                self,
                input_ids:torch.Tensor,
                attention_masks:torch.Tensor,
                token_type_ids:torch.Tensor,
                position_ids=None,
                head_mask=None,
                inputs_embeds=None,
                input_lengths=None,
                start_positions=None,
                end_positions=None,
    ):
        # TODO: figure out, why position IDs are necessary and how we can provide them
        # NOTE: token_type_ids == segment_ids
        bert_outputs = self.bert(
                            input_ids=input_ids,
                            token_type_ids=token_type_ids,
                            attention_mask=attention_masks,
                            position_ids=position_ids,
                            head_mask=head_mask
      )
      
        if self.qa_head_name == 'RecurrentQAHead':
            return self.qa_head(
                                bert_outputs=bert_outputs,
                                seq_lengths=input_lengths,
                                start_positions=start_positions,
                                end_positions=end_positions,
            )
        elif self.qa_head_name == 'LinearQAHead':
            return self.qa_head(
                                bert_outputs=bert_outputs,
                                start_positions=start_positions,
                                end_positions=end_positions,
            )