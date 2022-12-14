import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from torch_geometric.nn import GCNConv

import pdb

class FeedForwardNetwork(nn.Module):
    def __init__(self, hidden_size, ffn_size, dropout_rate):
        super(FeedForwardNetwork, self).__init__()

        self.layer1 = nn.Linear(hidden_size, ffn_size)
        self.gelu = nn.GELU()
        self.layer2 = nn.Linear(ffn_size, hidden_size)

    def reset_parameters(self):
        self.layer1.reset_parameters()
        self.layer2.reset_parameters()

    def forward(self, x):
        x = self.layer1(x)
        x = self.gelu(x)
        x = self.layer2(x)
        return x


class MultiHeadAttention(nn.Module):
    def __init__(self, hidden_size, attention_dropout_rate, num_heads):
        super(MultiHeadAttention, self).__init__()

        self.num_heads = num_heads

        self.att_size = att_size = hidden_size // num_heads
        self.scale = att_size ** -0.5

        self.linear_q = nn.Linear(hidden_size, num_heads * att_size)
        self.linear_k = nn.Linear(hidden_size, num_heads * att_size)
        self.linear_v = nn.Linear(hidden_size, num_heads * att_size)
        self.att_dropout = nn.Dropout(attention_dropout_rate)

        self.output_layer = nn.Linear(num_heads * att_size, hidden_size)

    def reset_parameters(self):
        self.linear_q.reset_parameters()
        self.linear_k.reset_parameters()
        self.linear_v.reset_parameters()
        self.output_layer.reset_parameters()

    def forward(self, q, k, v, attn_bias=None):
        orig_q_size = q.size()

        d_k = self.att_size
        d_v = self.att_size
        batch_size = q.size(0)

        # head_i = Attention(Q(W^Q)_i, K(W^K)_i, V(W^V)_i)
        q = self.linear_q(q).view(batch_size, -1, self.num_heads, d_k)
        k = self.linear_k(k).view(batch_size, -1, self.num_heads, d_k)
        v = self.linear_v(v).view(batch_size, -1, self.num_heads, d_v)

        q = q.transpose(1, 2)                  # [b, h, q_len, d_k]
        v = v.transpose(1, 2)                  # [b, h, v_len, d_v]
        k = k.transpose(1, 2).transpose(2, 3)  # [b, h, d_k, k_len]

        # Scaled Dot-Product Attention.
        # Attention(Q, K, V) = softmax((QK^T)/sqrt(d_k))V
        q = q * self.scale
        x = torch.matmul(q, k)  # [b, h, q_len, k_len]
        if attn_bias is not None:
            x = x + attn_bias

        x = torch.softmax(x, dim=3)
        x = self.att_dropout(x)
        x = x.matmul(v)  # [b, h, q_len, attn]

        x = x.transpose(1, 2).contiguous()  # [b, q_len, h, attn]
        x = x.view(batch_size, -1, self.num_heads * d_v)

        x = self.output_layer(x)

        assert x.size() == orig_q_size
        return x


class EncoderLayer(nn.Module):
    def __init__(self, hidden_size, ffn_size, dropout_rate, attention_dropout_rate, num_heads):
        super(EncoderLayer, self).__init__()

        self.self_attention_norm = nn.LayerNorm(hidden_size)
        self.self_attention = MultiHeadAttention(
            hidden_size, attention_dropout_rate, num_heads)
        self.self_attention_dropout = nn.Dropout(dropout_rate)

        self.ffn_norm = nn.LayerNorm(hidden_size)
        self.ffn = FeedForwardNetwork(hidden_size, ffn_size, dropout_rate)
        self.ffn_dropout = nn.Dropout(dropout_rate)

    def reset_parameters(self):
        self.self_attention_norm.reset_parameters()
        self.self_attention.reset_parameters()

        self.ffn_norm.reset_parameters()
        self.ffn.reset_parameters()

    def forward(self, x, attn_bias=None):
        y = self.self_attention_norm(x)
        y = self.self_attention(y, y, y, attn_bias)
        y = self.self_attention_dropout(y)
        x = x + y

        y = self.ffn_norm(x)
        y = self.ffn(y)
        y = self.ffn_dropout(y)
        x = x + y
        return x


class Graphormer(nn.Module):
    def __init__(
        self,
        n_layers,
        num_node_feat,
        num_heads,
        hidden_dim,
        ffn_dim,
        use_num_spd=False,
        use_cnb_jac=False,
        use_cnb_aa=False,
        use_degree=False,
        dropout_rate=0.1,
        intput_dropout_rate=0.1,
        attention_dropout_rate=0.1,
        multi_hop_max_dist=20,
    ):
        super(Graphormer, self).__init__()

        self.num_heads = num_heads
        self.atom_encoder = nn.Linear(num_node_feat, hidden_dim)
        #self.edge_encoder = nn.Embedding(64, num_heads, padding_idx=0)
        #self.edge_type = edge_type
        #if self.edge_type == 'multi_hop':
        #    self.edge_dis_encoder = nn.Embedding(
        #        40 * num_heads * num_heads, 1)
        self.len_shortest_path_encoder = nn.Embedding(40, num_heads, padding_idx=0)
        if use_num_spd:
            self.num_shortest_path_encoder = nn.Embedding(40, num_heads, padding_idx=0)
        if use_cnb_jac:
            self.undir_jac_encoder = nn.Embedding(40, num_heads, padding_idx=0)
        if use_cnb_aa:
            self.undir_aa_encoder = nn.Embedding(40, num_heads, padding_idx=0)
        if use_degree:
            self.in_degree_encoder = nn.Embedding(
                64, hidden_dim, padding_idx=0)
            self.out_degree_encoder = nn.Embedding(
                64, hidden_dim, padding_idx=0)

        self.input_dropout = nn.Dropout(intput_dropout_rate)
        encoders = [EncoderLayer(hidden_dim, ffn_dim, dropout_rate, attention_dropout_rate, num_heads)
                    for _ in range(n_layers)]
        self.layers = nn.ModuleList(encoders)
        self.final_ln = nn.LayerNorm(hidden_dim)

        self.graph_token = nn.Embedding(1, hidden_dim)
        self.graph_token_virtual_distance = nn.Embedding(1, num_heads)

        self.multi_hop_max_dist = multi_hop_max_dist
        self.hidden_dim = hidden_dim
        self.use_num_spd = use_num_spd
        self.use_cnb_jac = use_cnb_jac
        self.use_cnb_aa = use_cnb_aa
        self.use_degree = use_degree

    def reset_parameters(self):
        for layer in self.layers:
            layer.reset_parameters()
        self.final_ln.reset_parameters()
        self.atom_encoder.reset_parameters()
        #self.edge_encoder.reset_parameters()
        #self.edge_type = edge_type
        #if self.edge_type == 'multi_hop':
        #    self.edge_dis_encoder.reset_parameters()
        self.len_shortest_path_encoder.reset_parameters()
        if self.use_num_spd:
            self.num_shortest_path_encoder.reset_parameters()
        if self.use_cnb_jac:
            self.undir_jac_encoder.reset_parameters()
        if self.use_cnb_aa:
            self.undir_aa_encoder.reset_parameters()
        self.in_degree_encoder.reset_parameters()
        self.out_degree_encoder.reset_parameters()

    def forward(self, data, perturb=None):
        # attn_bias?????????????????????????????????????????????????????????????????????????????????(len_shortest_path_max)????????????-?????????????????????0????????????(n_graph, n_node+1, n_node+1)
        # len_shortest_path?????????????????????????????????????????????????????????(n_graph, n_node, n_node)
        # x????????????????????????????????????(n_graph, n_node, n_node_features)
        # in_degree????????????????????????????????????(n_graph, n_node)
        # out_degree????????????????????????????????????(n_graph, n_node)
        # edge_input???????????????????????????????????????(?????????????????????????????????multi_hop_max_dist)??????????????????????????????(n_graph, n_node, n_node, multi_hop_max_dist, n_edge_features)
        # attn_edge_type??????????????????????????????(n_graph, n_node, n_node, n_edge_features)

        x, edge_index = data.x, data.edge_index
        attn_bias = data.attn_bias
        len_shortest_path = torch.clamp(data.len_shortest_path, min=0, max=39).long()
        in_degree = torch.clamp(data.in_degree, min=0, max=63).long()
        out_degree = torch.clamp(data.out_degree, min=0, max=63).long()
        #edge_input = data.edge_input

        # graph_attn_bias
        # ?????????????????????????????????????????????????????????????????????????????????
        n_graph, n_node = x.size()[:2]
        graph_attn_bias = attn_bias.clone()
        graph_attn_bias = graph_attn_bias.unsqueeze(1).repeat(
            1, self.num_heads, 1, 1)  # [n_graph, n_head, n_node+1, n_node+1]

        # spatial pos
        # ????????????,??????????????????????????????????????????????????????
        # [n_graph, n_node, n_node, n_head] -> [n_graph, n_head, n_node, n_node]
        spatial_pos_bias = self.len_shortest_path_encoder(len_shortest_path).permute(0, 3, 1, 2)
        if self.use_num_spd:
            num_shortest_path = torch.clamp(data.num_shortest_path, min=0, max=39).long()
            spatial_pos_bias = spatial_pos_bias + self.num_shortest_path_encoder(num_shortest_path.long()).permute(0, 3, 1, 2)
        if self.use_cnb_jac:
            undir_jac = data.undir_jac
            undir_jac_enc = torch.clamp(undir_jac*30, min=0, max=39).long()
            spatial_pos_bias = spatial_pos_bias + self.undir_jac_encoder(undir_jac_enc).permute(0, 3, 1, 2)
        if self.use_cnb_aa:
            undir_aa = data.undir_aa
            undir_aa_enc = torch.clamp(undir_aa*10, min=0, max=39).long()
            spatial_pos_bias = spatial_pos_bias + self.undir_aa_encoder(undir_aa_enc).permute(0, 3, 1, 2)
        graph_attn_bias[:, :, 1:, 1:] = graph_attn_bias[:,
                                                        :, 1:, 1:] + spatial_pos_bias

        # reset spatial pos here
        # ???????????????????????????????????????????????????????????????????????????????????????????????????????????????1
        t = self.graph_token_virtual_distance.weight.view(1, self.num_heads, 1)
        graph_attn_bias[:, :, 1:, 0] = graph_attn_bias[:, :, 1:, 0] + t
        graph_attn_bias[:, :, 0, :] = graph_attn_bias[:, :, 0, :] + t

        graph_attn_bias = graph_attn_bias + attn_bias.unsqueeze(1)  # reset

        # node feauture + graph token
        node_feature = self.atom_encoder(x)           # [n_graph, n_node, n_hidden]

        # ??????????????????????????????????????????????????????????????????????????????????????????????????????????????????
        if self.use_degree:
            node_feature = node_feature + \
                self.in_degree_encoder(in_degree) + \
                self.out_degree_encoder(out_degree)
        graph_token_feature = self.graph_token.weight.unsqueeze(
            0).repeat(n_graph, 1, 1)
        graph_node_feature = torch.cat(
            [graph_token_feature, node_feature], dim=1)

        # transfomrer encoder
        output = self.input_dropout(graph_node_feature)
        for enc_layer in self.layers:
            output = enc_layer(output, graph_attn_bias)
        output = self.final_ln(output)

        return output
