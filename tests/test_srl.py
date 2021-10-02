import sys

import torch
import torch.nn.functional as F

import pytest
from pylon.constraint import constraint
from pylon.tnorm_solver import *
from pylon.sampling_solver import WeightedSamplingSolver
#from pylon.circuit_solver import SemanticLossCircuitSolver
from pylon.shaped_lazy_solver import TNormSolver as LazyTNormSolver
from pylon.shaped_lazy_solver import ProductTNormSolver as LazyProductTNormSolver
from pylon.shaped_lazy_solver import GodelTNormSolver as LazyGodelTNormSolver
from pylon.shaped_lazy_solver import LukasiewiczTNormSolver as LazyLukasiewiczTNormSolver

LABELS = ['O', 'B-A0', 'I-A0', 'B-A1', 'I-A1', 'B-A2', 'B-A3', 'B-V']
LABEL_TO_ID = {p: i for i, p in enumerate(LABELS)}

# TODO, add more solvers
def get_solvers(num_samples):
    return [ProductTNormLogicSolver(), GodelTNormLogicSolver(), LukasiewiczTNormLogicSolver(), \
        WeightedSamplingSolver(num_samples), \
        LazyProductTNormSolver(), LazyGodelTNormSolver(), LazyLukasiewiczTNormSolver()]


class SRL_NET(torch.nn.Module):
    '''Simple SRL model'''

    def __init__(self, vocab_size, num_label, hidden_dim=50, embedding_dim=100):
        super().__init__()

        self.vocab_size = vocab_size
        self.hidden_dim = hidden_dim
        self.embedding_dim = embedding_dim

        # layers
        self.embedding = torch.nn.Embedding(self.vocab_size, self.embedding_dim)
        #self.embedding.weight = torch.nn.Parameter(vocab.vectors)
        self.embedding.weight.data.uniform_(-1.0, 1.0)

        self.lstm = torch.nn.LSTM(self.embedding_dim, self.hidden_dim, batch_first=True)
        self.fc = torch.nn.Linear(self.hidden_dim, num_label)

        # Initialize fully connected layer
        self.fc.bias.data.fill_(0)
        torch.nn.init.xavier_uniform_(self.fc.weight, gain=1)

    def forward(self, s):
        s = self.embedding(s)   # dim: batch_size x batch_max_len x embedding_dim
        s, _ = self.lstm(s)     # dim: batch_size x batch_max_len x lstm_hidden_dim
        s = self.fc(s)          # dim: batch_size x batch_max_len x num_label

        return s

# any core argument (B-A*) excludes other tokens to be any core argument (B-A*)
# assuming the input y has shape (batch_size, seq_len, num_label)
#   and y_ext has shape (batch_size, seq_len, seq_len, num_label) and the diagonal of the middle two dims are masked out by -inf
def unique_role(y, y_ext):
    #return (y == 1) <= (not (y_ext == 1 or y_ext == 5)).all(2)
    b_a0 = (y == 1) <= (y_ext == 1).logical_not().all(2)    # can't do -1, needs ast.USub; and can't do all() since IsEq doesn't squeeze
    b_a1 = (y == 3) <= (y_ext == 3).logical_not().all(2)
    b_a2 = (y == 5) <= (y_ext == 5).logical_not().all(2)
    b_a3 = (y == 6) <= (y_ext == 6).logical_not().all(2)
    return b_a0.logical_and(b_a1).logical_and(b_a2).logical_and(b_a3).all(1)


def unique_role_sampling(y, y_ext):
    return unique_role(y, y_ext)

def unique_role_lazy(y):
    from pylon import lazy_torch as torch
    shape = y.size()
    # different from non-lazy tensor versions, here we can create the y_ext inside of the constraint function
    # y of shape (batch_l, seq_l, num_label)
    #   for each token with a core role label, we want it to imply other tokens not to have the same core label
    #   we will do this efficiently using y_ext which maps each token to all other tokens (so that we can block the same core label on them)
    # y_ext of shape (batch_l, seq_l, seq_l, num_label), 
    #   created by y.view(batch_l, 1, seq_l, num_label) and then repeat at the dim=1 for seq_l times
    #   then mask out the diagonal of dim 1 and 2 (i.e. force them to have 0 probabilities)
    #       so that the token i with a core role label does not negates itself
    # note that y is already probabilities here (not log scale)
    #   so force them to be large negative at log scale and then exp
    y_ext = y + 1e-8
    y_ext = y_ext.unsqueeze(1).tile(1, shape[1], 1, 1).log()
    y_ext = y_ext + torch.eye(shape[1]).unsqueeze(0).unsqueeze(3).tile(shape[0], 1, 1, shape[2]) * -1e6
    y_ext = y_ext.exp()

    #y: batch_size, seq_l, num_label
    #y_ext: batch_size, seq_l, seq_l, num_label
    b_a0 = y[:, :, 1] <= y_ext[:, :, :, 1].logical_not().all(2)    # can't do -1, needs ast.USub; and can't do all() since IsEq doesn't squeeze
    b_a1 = y[:, :, 3] <= y_ext[:, :, :, 3].logical_not().all(2)
    b_a2 = y[:, :, 5] <= y_ext[:, :, :, 5].logical_not().all(2)
    b_a3 = (y[:, :, 6]) <= y_ext[:, :, :, 6].logical_not().all(2)
    return b_a0.logical_and(b_a1).logical_and(b_a2).logical_and(b_a3).all(1)


# DEPRECATED
#   Keep the code here just to show a working flow
# only meant for SamplingSolver
# y has size (batch_size, seq_len) representing discrete labels
def unique_role_sampling_(y):
    #   y_ext has size (batch_size, seq_len, seq_len) representing discrete labels as well,
    #       and the diagonal of the last two dims are masked out by -inf
    #   Note that y_ext has to be created here, otherwise it will be sampled in sampling solver.
    batch_l, seq_l = y.shape
    diag = 1 - torch.eye(seq_l).view(1, seq_l, seq_l).expand(batch_l, seq_l, seq_l)
    y_ext = y * diag

    b_a0 = (y == 1) <= (y_ext == 1).logical_not().all(2)    # can't do -1, needs ast.USub; and can't do all() since IsEq doesn't squeeze
    b_a1 = (y == 3) <= (y_ext == 3).logical_not().all(2)
    b_a2 = (y == 5) <= (y_ext == 5).logical_not().all(2)
    b_a3 = (y == 6) <= (y_ext == 6).logical_not().all(2)
    return b_a0.logical_and(b_a1).logical_and(b_a2).logical_and(b_a3)

# inputs are binary masks where 1 is the spiked label and 0's are the rest
def unique_role_check(y_mask):
    batch_l, seq_l = y_mask.shape
    for i in range(batch_l):
        assert((y_mask[i] == 1).sum() <= 1)
        assert((y_mask[i] == 3).sum() <= 1)
        assert((y_mask[i] == 5).sum() <= 1)
        assert((y_mask[i] == 6).sum() <= 1)


def train(data, constraint):
    num_label = len(LABEL_TO_ID)
    srl = SRL_NET(vocab_size=3027, num_label=num_label)

    opt = torch.optim.SGD(list(srl.parameters()), lr=1.0)

    tokens, y = data

    for i in range(10):
        opt.zero_grad()

        logits = srl(tokens)

        batch_l, seq_l, _ = logits.shape

        yloss = F.cross_entropy(logits.view(-1, num_label), y.view(-1))

        if constraint.cond is unique_role_lazy:
            closs = constraint(logits)
        else:
            logits_ext = logits.unsqueeze(2).expand(batch_l, seq_l, seq_l, num_label)
            diag_mask = torch.eye(seq_l).view(1, seq_l, seq_l, 1).expand_as(logits_ext)
            logits_ext = logits_ext + diag_mask * -1e6
            closs = constraint(logits, logits_ext)

        loss = 0.5 * closs + 0.95 * yloss

        loss.backward()
        opt.step()

    return srl

#@pytest.mark.skip(reason="Does not conform to expected output shape of constraint function")
def test_srl():
    tokens, y = get_data()

    for solver in get_solvers(num_samples=50):

        if issubclass(solver.__class__, LazyTNormSolver):
            constr_func = unique_role_lazy
        elif isinstance(solver, WeightedSamplingSolver):
            constr_func = unique_role_sampling
        else:
            constr_func = unique_role

        cons = constraint(constr_func, solver)
        srl = train([tokens, y], cons)

        y_ = torch.softmax(srl(tokens).view(-1, len(LABEL_TO_ID)), dim=-1)
        y_mask = (y_ == y_.max(-1)[0].unsqueeze(-1))
        
        unique_role_check(y_mask)


def get_data():
    toks = torch.tensor([[32, 1973, 2272,   15,    3,    10,    0,    5,    0,  389]])

    y = torch.tensor([[0, 1, 2, 2, 0, 7, 0, 1, 3, 1]])

    return toks, y

