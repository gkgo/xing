import torch
import torch.nn as nn
import torch.nn.functional as F


from models.resnet import ResNet
from models.cca import *
from models.scr import  *
import numpy as np
# from models.others.se import SqueezeExcitation
# from models.others.lsa import LocalSelfAttention
# from models.others.nlsa import NonLocalSelfAttention
# from models.others.sce import SpatialContextEncoder


class RENet(nn.Module):

    def __init__(self, args, mode=None):
        super().__init__()
        self.mode = mode
        self.args = args

        self.encoder = ResNet(args=args)
        # self.non_local = nonLocal(channel=640)
        self.encoder_dim = 640

        self.fc = nn.Linear(self.encoder_dim, self.args.num_class)
        self.lin = nn.Linear(25, 5)

        self.scr_module = self._make_scr_layer(planes=[640, 64, 64, 64, 640])
        self.match_net = match_block(640)
        self.match_net1 = match_block1(640)
        self.cca_module = CCA(kernel_sizes=[3, 3], planes=[16, 1])
        self.cca_1x1 = nn.Sequential(
            nn.Conv2d(self.encoder_dim, 64, kernel_size=1, bias=False),
            nn.BatchNorm2d(64),
            nn.ReLU()
        )



    def _make_scr_layer(self, planes):
        stride, kernel_size, padding = (1, 1, 1), (5, 5), 2
        layers = list()

        if self.args.self_method == 'scr':
            # corr_block1 = SelfCorrelationComputation1(d_model=640, h=1)
            # corr_block = SelfCorrelationComputation(kernel_size=kernel_size, padding=padding)
            # self_block = SCR(planes=planes, stride=stride)
            # corr_block2 = SelfCorrelationComputation6(in_planes=640, out_planes=640)
            corr_block2 = SelfCorrelationComputation5(in_channels=640, out_channels=640)
            # corr_block2 = SelfCorrelationComputation4(channel=640)
            # corr_block2 = SelfCorrelationComputation3(in_channels=640)
            # corr_block2 = SelfCorrelationComputation2(in_channels=640, out_channels=640, kernel_size=5)
        # elif self.args.self_method == 'sce':
        #     planes = [640, 64, 64, 640]
        #     self_block = SpatialContextEncoder(planes=planes, kernel_size=kernel_size[0])
        # elif self.args.self_method == 'se':
        #     self_block = SqueezeExcitation(channel=planes[0])
        # elif self.args.self_method == 'lsa':
        #     self_block = LocalSelfAttention(in_channels=planes[0], out_channels=planes[0], kernel_size=kernel_size[0])
        # elif self.args.self_method == 'nlsa':
        #     self_block = NonLocalSelfAttention(planes[0], sub_sample=False)
        else:
            raise NotImplementedError

        if self.args.self_method == 'scr':
            layers.append(corr_block2)
        #     layers.append(corr_block)
        # layers.append(self_block)
        return nn.Sequential(*layers)

    def forward(self, input):
        if self.mode == 'fc':
            return self.fc_forward(input)
        elif self.mode == 'encoder':
            return self.encode(input, False)
        elif self.mode == 'cca':
            spt, qry = input
            return self.cca(spt, qry)
        else:
            raise ValueError('Unknown mode')

    def fc_forward(self, x):
        x = x.mean(dim=[-1, -2])
        return self.fc(x)

    def cca(self, spt, qry):  # ???????????????

        spt = spt.squeeze(0)  # ????????????????????????1?????????

        spt = self.normalize_feature(spt)  # 1
        qry = self.normalize_feature(qry)
        # spt, qry = self.match_net(spt, qry)


        corr4d = self.get_4d_correlation_map(spt, qry)  # 10???5???5???5???5???5
        num_qry, way, H_s, W_s, H_q, W_q = corr4d.size()

        # corr4d refinement
        # corr4d = self.cca_module(corr4d.view(-1, 1, H_s, W_s, H_q, W_q))
        corr4d_s = corr4d.view(num_qry, way, H_s * W_s, H_q, W_q)  # 10???5???25???5???5
        corr4d_q = corr4d.view(num_qry, way, H_s, W_s, H_q * W_q)  # 10???5???5???5???25

        # normalizing the entities for each side to be zero-mean and unit-variance to stabilize training
        corr4d_s = self.gaussian_normalize(corr4d_s, dim=2)
        corr4d_q = self.gaussian_normalize(corr4d_q, dim=4)

        # applying softmax for each side
        corr4d_s = F.softmax(corr4d_s / self.args.temperature_attn, dim=2)
        corr4d_s = corr4d_s.view(num_qry, way, H_s, W_s, H_q, W_q)  # 10???5???5???5???5???5
        corr4d_q = F.softmax(corr4d_q / self.args.temperature_attn, dim=4)
        corr4d_q = corr4d_q.view(num_qry, way, H_s, W_s, H_q, W_q)  # 10???5???5???5???5???5

        # suming up matching scores
        attn_s = corr4d_s.sum(dim=[4, 5])  # 10???5???5???5
        attn_q = corr4d_q.sum(dim=[2, 3])  # 10???5???5???5

        # applying attention
        spt_attended = attn_s.unsqueeze(2) * spt.unsqueeze(0)  # 10???5???640???5???5
        spt_attended = spt_attended.view(-1,640,H_s, W_s)
        qry_attended = attn_q.unsqueeze(2) * qry.unsqueeze(1)  # 10???5???640???5???5
        qry_attended = qry_attended.view(-1,640,H_q, W_q)
        spt_attended, qry_attended = self.match_net(spt_attended, qry_attended )
        spt_attended = spt_attended.view(num_qry, way,640,H_s, W_s)
        qry_attended = qry_attended.view(num_qry, way,640,H_q, W_q)

        # averaging embeddings for k > 1 shots
        if self.args.shot > 1:
            spt_attended = spt_attended.view(num_qry, self.args.shot, self.args.way, *spt_attended.shape[2:])
            qry_attended = qry_attended.view(num_qry, self.args.shot, self.args.way, *qry_attended.shape[2:])
            spt_attended = spt_attended.mean(dim=1)
            qry_attended = qry_attended.mean(dim=1)

        # In the main paper, we present averaging in Eq.(4) and summation in Eq.(5).
        # In the implementation, the order is reversed, however, those two ways become eventually the same anyway :)
        spt_attended_pooled = spt_attended.mean(dim=[-1, -2])
        qry_attended_pooled = qry_attended.mean(dim=[-1, -2])
        qry_pooled = qry.mean(dim=[-1, -2])
        similarity_matrix = F.cosine_similarity(spt_attended_pooled, qry_attended_pooled, dim=-1)


        # for x,y in zip(batch1,batch2):
        #     act_det = x
        #     act_aim = y
        #     bs, cs, height_a, width_a = act_aim.shape  # ?????????
        #     bq, cq, height_d, width_d = act_det.shape  # ?????????
        #     act_aim = act_aim.view(bs, -1, height_a * width_a)
        #     act_det = act_det.view(bq, -1, height_d * width_d)
        #     act_det = self.lin(act_det)
        #     act_aim = self.lin(act_aim)
        #     similarity_matrix = F.cosine_similarity(act_aim, act_det, dim=1)
        #     cos1.append(similarity_matrix)
        # similarity_matrix1 = torch.cat((cos1), dim=0)
        #------------------------------------------------------------------------------------

        # batch1 = []  # ??????
        # batch2 = []  # ??????
        # cos = []
        # if self.args.shot > 1:
        #     qry_1, qry_2, qry_3 = torch.chunk(qry, 3, dim=0)
        #     ch = [qry_1, qry_2, qry_3]
        #     for d in zip(ch):
        #         cx = d
        #         cx = torch.tensor(np.array([item.cpu().detach().numpy() for item in cx])).cuda()
        #         cx = cx.squeeze(0)
        #         act_det, act_aim = self.match_net(spt, cx)
        #         batch1.append(act_det)
        #         batch2.append(act_aim)
        # else:
        #     qry_1, qry_2,qry_3, qry_4,qry_5, qry_6,qry_7, qry_8,qry_9, qry_10,qry_11, qry_12 ,qry_13, qry_14,qry_15= torch.chunk(qry, 15, dim=0)
        #     ch = [qry_1, qry_2,qry_3, qry_4,qry_5, qry_6,qry_7, qry_8,qry_9, qry_10,qry_11, qry_12 ,qry_13, qry_14,qry_15]
        #     for d in zip(ch):
        #         cx = d
        #         cx = torch.tensor(np.array([item.cpu().detach().numpy() for item in cx])).cuda()
        #         cx = cx.squeeze(0)
        #         act_det, act_aim = self.match_net(spt, cx)
        #         batch1.append(act_det)
        #         batch2.append(act_aim)

        # batch1 = []  # ??????
        # batch2 = []  # ??????
        # cos = []
        # qry_1, qry_2 = torch.chunk(qry, 2, dim=0)
        # ch = [qry_1, qry_2]
        # for d in zip(ch):
        #     cx = d
        #     cx = torch.tensor(np.array([item.cpu().detach().numpy() for item in cx])).cuda()
        #     cx = cx.squeeze(0)
        #     act_det, act_aim = self.match_net(spt, cx)
        #     batch1.append(act_det)
        #     batch2.append(act_aim)



        # act_det = self.lin(act_det)
        # act_aim = self.lin(act_aim)
        #
        # similarity_matrix = F.cosine_similarity(act_aim, act_det, dim=1)  # ??????3???75???5???
        # # ?????????????????????????????????????????????
        # if self.training:
        #     return similarity_matrix / self.args.temperature, self.fc(qry_pooled)
        # else:
        #     return similarity_matrix / self.args.temperature

# -----------------------------------------------------------------------


        # (S * C * Hs * Ws, Q * C * Hq * Wq) -> Q * S * Hs * Ws * Hq * Wq
        # for x,y,i in zip(batch1,batch2,ch):  # ?????? ??????
        #     act_det = x
        #     act_aim = y
        #     QR = i
        #     QR = torch.tensor(np.array([item.cpu().detach().numpy() for item in QR])).cuda()
        #     QR = QR.squeeze(0)
        #     corr4d = self.get_4d_correlation_map(act_aim,  act_det)
        #     num_qry, way, H_s, W_s, H_q, W_q = corr4d.size()
        #
        #     # corr4d refinement
        #     corr4d = self.cca_module(corr4d.view(-1, 1, H_s, W_s, H_q, W_q))  # ???375???1???5???5???5???5?????????4??????????????????
        #     corr4d_s = corr4d.view(num_qry, way, H_s * W_s, H_q, W_q)  # ???75???5???25???5???5???
        #     corr4d_q = corr4d.view(num_qry, way, H_s, W_s, H_q * W_q)  # ???75???5???5???5???25???
        #
        #     # normalizing the entities for each side to be zero-mean and unit-variance to stabilize training
        #     corr4d_s = self.gaussian_normalize(corr4d_s, dim=2)  # H_q * W_q??????????????????????????????  # (5,5,25,5,5)
        #     corr4d_q = self.gaussian_normalize(corr4d_q, dim=4)  # ???H_q * W_q?????????????????????
        #
        #     # applying softmax for each side
        #     corr4d_s = F.softmax(corr4d_s / self.args.temperature_attn, dim=2)  # Eq.4??????????????????????????????# (5,5,25,5,5)
        #     corr4d_s = corr4d_s.view(num_qry, way, H_s, W_s, H_q, W_q)
        #     corr4d_q = F.softmax(corr4d_q / self.args.temperature_attn, dim=4)
        #     corr4d_q = corr4d_q.view(num_qry, way, H_s, W_s, H_q, W_q)
        #
        #     # suming up matching scores
        #     attn_s = corr4d_s.sum(dim=[4, 5])  # ??????2??? ?????????????????????
        #     attn_q = corr4d_q.sum(dim=[2, 3])  # ??????2???
        #     # ????????????5???5???5???5???
        #     # applying attention
        #     spt_attended = attn_s.unsqueeze(2) * spt.unsqueeze(0)  # ??????5????????????query embedding
        #     qry_attended = attn_q.unsqueeze(2) * QR.unsqueeze(1)
        #     # ???5???5???640???5???5???
        #     # averaging embeddings for k > 1 shots
        #     if self.args.shot > 1:
        #         spt_attended = spt_attended.view(num_qry, self.args.shot, self.args.way, *spt_attended.shape[2:])
        #         qry_attended = qry_attended.view(num_qry, self.args.shot, self.args.way, *qry_attended.shape[2:])
        #         spt_attended = spt_attended.mean(dim=1)
        #         qry_attended = qry_attended.mean(dim=1)
        #
        #     # In the main paper, we present averaging in Eq.(4) and summation in Eq.(5).
        #     # In the implementation, the order is reversed, however, those two ways become eventually the same anyway :)
        #     spt_attended_pooled = spt_attended.mean(dim=[-1, -2])
        #     qry_attended_pooled = qry_attended.mean(dim=[-1, -2])
        #
        #
        #     similarity_matrix = F.cosine_similarity(spt_attended_pooled, qry_attended_pooled, dim=-1)  # ??????3???75???5???
        #     # ?????????????????????????????????????????????
        #     cos.append(similarity_matrix)
        #
        # similarity_matrix = torch.cat((cos),dim=0)
        # # ????????????????????????4??????1/h*w??????75???5???640???
        # qry_pooled = qry.mean(dim=[-1, -2])
        if self.training:
            return similarity_matrix / self.args.temperature, self.fc(qry_pooled)
        else:
            return similarity_matrix / self.args.temperature


# ----------------------------------------------------------------------------------
    def gaussian_normalize(self, x, dim, eps=1e-05):
        x_mean = torch.mean(x, dim=dim, keepdim=True)
        x_var = torch.var(x, dim=dim, keepdim=True)  # ???dim????????????
        x = torch.div(x - x_mean, torch.sqrt(x_var + eps))  # ???x??????-x?????????/?????????x_var
        return x

    def get_4d_correlation_map(self, spt, qry):
        '''
        The value H and W both for support and query is the same, but their subscripts are symbolic.
        :param spt: way * C * H_s * W_s
        :param qry: num_qry * C * H_q * W_q
        :return: 4d correlation tensor: num_qry * way * H_s * W_s * H_q * W_q
        :rtype:
        '''
        way = spt.shape[0]
        num_qry = qry.shape[0]

        # reduce channel size via 1x1 conv??????????????????
        spt = self.cca_1x1(spt)  # 5,64,5,5
        qry = self.cca_1x1(qry)  # 10,64,5,5

        # normalize channels for later cosine similarity
        spt = F.normalize(spt, p=2, dim=1, eps=1e-8)
        qry = F.normalize(qry, p=2, dim=1, eps=1e-8)

        # num_way * C * H_p * W_p --> num_qry * way * H_p * W_p
        # num_qry * C * H_q * W_q --> num_qry * way * H_q * W_q
        spt = spt.unsqueeze(0).repeat(num_qry, 1, 1, 1, 1)  # ???0???????????????num_qry 10???5???64???5???5
        qry = qry.unsqueeze(1).repeat(1, way, 1, 1, 1)  # ????????????????????????way
        # ????????????????????????75???5???64???5???5???
        similarity_map_einsum = torch.einsum('qncij,qnckl->qnijkl', spt, qry)  # ???75???5???5???5???5???5???
        # 2 ????????????????????????
        return similarity_map_einsum

    def normalize_feature(self, x):
        return x - x.mean(1).unsqueeze(1)  # x-x.mean(1)?????????????????????channal????????????????????????

    def encode(self, x, do_gap=True):
        x = self.encoder(x)
        # x = self.non_local(x)

        if self.args.self_method:
            identity = x  # (80,640,5,5)
            x = self.scr_module(x)
            # x = self.match_net1(x,identity)

            if self.args.self_method == 'scr':
                x = x + identity   # ?????????2???
            x = F.relu(x, inplace=True)

        if do_gap:
            return F.adaptive_avg_pool2d(x, 1)
        else:
            return x

# if __name__ == '__main__':
#     args = setup_run(arg_mode='train')  # ????????????args
#     set_seed(args.seed)
#     model = RENet(args).cuda()  # ????????????model?????????????????????GPU???(??????renet)
#     model = nn.DataParallel(model, device_ids=args.device_ids)  # ????????????GPU????????????GPU?????????
#
#     if not args.no_wandb:
#         wandb.watch(model)
#     print(model)  # ??????wandb????????????????????????
