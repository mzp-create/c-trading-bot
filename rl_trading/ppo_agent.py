"""
Proximal Policy Optimization (PPO) Agent
For crypto trading with discrete action space
"""

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.distributions import Categorical
from typing import List, Tuple, Dict
import pickle
from pathlib import Path


class ActorCritic(nn.Module):
    """
    Actor-Critic network with LSTM for sequential data
    """
    
    def __init__(
        self,
        input_dim: int,
        hidden_dim: int = 256,
        lstm_dim: int = 128,
        n_actions: int = 9,
        dropout: float = 0.2
    ):
        super(ActorCritic, self).__init__()
        
        self.lstm_dim = lstm_dim
        
        # LSTM for sequential input
        self.lstm = nn.LSTM(
            input_size=input_dim,
            hidden_size=lstm_dim,
            num_layers=2,
            batch_first=True,
            dropout=dropout
        )
        
        # Shared feature extractor
        self.shared = nn.Sequential(
            nn.Linear(lstm_dim, hidden_dim),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU()
        )
        
        # Actor head (policy)
        self.actor = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim // 2),
            nn.ReLU(),
            nn.Linear(hidden_dim // 2, n_actions)
        )
        
        # Critic head (value function)
        self.critic = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim // 2),
            nn.ReLU(),
            nn.Linear(hidden_dim // 2, 1)
        )
        
        self._init_weights()
    
    def _init_weights(self):
        """Initialize weights"""
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.orthogonal_(m.weight, gain=np.sqrt(2))
                nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.LSTM):
                for name, param in m.named_parameters():
                    if 'weight' in name:
                        nn.init.orthogonal_(param)
                    elif 'bias' in name:
                        nn.init.constant_(param, 0)
    
    def forward(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Forward pass
        
        Args:
            x: Input tensor of shape (batch, seq_len, features)
        
        Returns:
            action_logits: (batch, n_actions)
            value: (batch, 1)
        """
        # LSTM forward
        lstm_out, _ = self.lstm(x)
        # Take last timestep output
        lstm_out = lstm_out[:, -1, :]
        
        # Shared features
        features = self.shared(lstm_out)
        
        # Actor and critic outputs
        action_logits = self.actor(features)
        value = self.critic(features)
        
        return action_logits, value
    
    def get_action(self, state: np.ndarray, deterministic: bool = False) -> Tuple[int, float, torch.Tensor]:
        """
        Get action from policy
        
        Args:
            state: State array (seq_len, features)
            deterministic: If True, return argmax action
        
        Returns:
            action: Selected action index
            log_prob: Log probability of action
            value: State value estimate
        """
        with torch.no_grad():
            state_tensor = torch.FloatTensor(state).unsqueeze(0).to(next(self.parameters()).device)
            action_logits, value = self.forward(state_tensor)
            
            if deterministic:
                action = torch.argmax(action_logits, dim=1).item()
                log_prob = torch.log_softmax(action_logits, dim=1)[0, action].item()
            else:
                dist = Categorical(logits=action_logits)
                action = dist.sample().item()
                log_prob = dist.log_prob(torch.tensor(action)).item()
        
        return action, log_prob, value.squeeze().cpu()


class ReplayBuffer:
    """Buffer for storing trajectories"""
    
    def __init__(self):
        self.states = []
        self.actions = []
        self.rewards = []
        self.log_probs = []
        self.values = []
        self.dones = []
    
    def add(self, state, action, reward, log_prob, value, done):
        self.states.append(state)
        self.actions.append(action)
        self.rewards.append(reward)
        self.log_probs.append(log_prob)
        self.values.append(value)
        self.dones.append(done)
    
    def clear(self):
        self.states.clear()
        self.actions.clear()
        self.rewards.clear()
        self.log_probs.clear()
        self.values.clear()
        self.dones.clear()
    
    def get(self) -> Dict:
        return {
            'states': np.array(self.states),
            'actions': np.array(self.actions),
            'rewards': np.array(self.rewards),
            'log_probs': np.array(self.log_probs),
            'values': np.array(self.values),
            'dones': np.array(self.dones)
        }
    
    def __len__(self):
        return len(self.states)


class PPOAgent:
    """
    PPO Agent for trading
    """
    
    def __init__(
        self,
        state_dim: int,
        n_actions: int = 15,  # Updated for short-selling support
        lr: float = 3e-4,
        gamma: float = 0.99,
        gae_lambda: float = 0.95,
        clip_epsilon: float = 0.2,
        value_coef: float = 0.5,
        entropy_coef: float = 0.01,
        device: str = 'auto'
    ):
        self.state_dim = state_dim
        self.n_actions = n_actions
        self.gamma = gamma
        self.gae_lambda = gae_lambda
        self.clip_epsilon = clip_epsilon
        self.value_coef = value_coef
        self.entropy_coef = entropy_coef
        self.max_grad_norm = 0.5
        
        # Device
        if device == 'auto':
            self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        else:
            self.device = torch.device(device)
        
        # Network
        self.network = ActorCritic(
            input_dim=state_dim,
            n_actions=n_actions
        ).to(self.device)
        
        # Optimizer
        self.optimizer = optim.Adam(self.network.parameters(), lr=lr, eps=1e-5)
        
        # Training stats
        self.training_step = 0
        
    def select_action(self, state: np.ndarray, deterministic: bool = False) -> Tuple[int, float, float]:
        """Select action from current policy"""
        action, log_prob, value = self.network.get_action(state, deterministic)
        return action, log_prob, value.item()
    
    def compute_gae(
        self,
        rewards: np.ndarray,
        values: np.ndarray,
        dones: np.ndarray,
        next_value: float
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Compute Generalized Advantage Estimation
        
        Returns:
            advantages: Advantage estimates
            returns: TD-lambda returns
        """
        advantages = np.zeros_like(rewards)
        last_gae = 0
        
        for t in reversed(range(len(rewards))):
            if t == len(rewards) - 1:
                next_val = next_value
            else:
                next_val = values[t + 1]
            
            delta = rewards[t] + self.gamma * next_val * (1 - dones[t]) - values[t]
            last_gae = delta + self.gamma * self.gae_lambda * (1 - dones[t]) * last_gae
            advantages[t] = last_gae
        
        returns = advantages + values
        return advantages, returns
    
    def update(
        self,
        buffer: ReplayBuffer,
        next_state: np.ndarray,
        n_epochs: int = 4,
        batch_size: int = 64
    ) -> Dict:
        """
        Update policy using PPO
        
        Args:
            buffer: Replay buffer with trajectory
            next_state: Next state for bootstrapping
            n_epochs: Number of optimization epochs
            batch_size: Mini-batch size
        
        Returns:
            Dictionary of training metrics
        """
        if len(buffer) < batch_size:
            return {}
        
        # Get buffer data
        data = buffer.get()
        states = torch.FloatTensor(data['states']).to(self.device)
        actions = torch.LongTensor(data['actions']).to(self.device)
        old_log_probs = torch.FloatTensor(data['log_probs']).to(self.device)
        rewards = data['rewards']
        values = data['values']
        dones = data['dones']
        
        # Get next value for GAE
        with torch.no_grad():
            next_state_tensor = torch.FloatTensor(next_state).unsqueeze(0).to(self.device)
            _, next_value = self.network(next_state_tensor)
            next_value = next_value.squeeze().cpu().item()
        
        # Compute advantages and returns
        advantages, returns = self.compute_gae(rewards, values, dones, next_value)
        advantages = torch.FloatTensor(advantages).to(self.device)
        returns = torch.FloatTensor(returns).to(self.device)
        
        # Normalize advantages
        advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-8)
        
        # PPO update
        total_policy_loss = 0
        total_value_loss = 0
        total_entropy = 0
        n_updates = 0
        
        for epoch in range(n_epochs):
            # Create random mini-batches
            indices = np.random.permutation(len(states))
            
            for start_idx in range(0, len(states), batch_size):
                end_idx = min(start_idx + batch_size, len(states))
                batch_indices = indices[start_idx:end_idx]
                
                batch_states = states[batch_indices]
                batch_actions = actions[batch_indices]
                batch_old_log_probs = old_log_probs[batch_indices]
                batch_advantages = advantages[batch_indices]
                batch_returns = returns[batch_indices]
                
                # Forward pass
                action_logits, batch_values = self.network(batch_states)
                dist = Categorical(logits=action_logits)
                
                # New log probs
                new_log_probs = dist.log_prob(batch_actions)
                
                # PPO policy loss
                ratio = torch.exp(new_log_probs - batch_old_log_probs)
                surr1 = ratio * batch_advantages
                surr2 = torch.clamp(ratio, 1 - self.clip_epsilon, 1 + self.clip_epsilon) * batch_advantages
                policy_loss = -torch.min(surr1, surr2).mean()
                
                # Value loss
                value_loss = nn.MSELoss()(batch_values.squeeze(), batch_returns)
                
                # Entropy bonus
                entropy = dist.entropy().mean()
                
                # Total loss
                loss = policy_loss + self.value_coef * value_loss - self.entropy_coef * entropy
                
                # Optimize
                self.optimizer.zero_grad()
                loss.backward()
                nn.utils.clip_grad_norm_(self.network.parameters(), self.max_grad_norm)
                self.optimizer.step()
                
                total_policy_loss += policy_loss.item()
                total_value_loss += value_loss.item()
                total_entropy += entropy.item()
                n_updates += 1
        
        self.training_step += 1
        
        return {
            'policy_loss': total_policy_loss / n_updates,
            'value_loss': total_value_loss / n_updates,
            'entropy': total_entropy / n_updates,
            'mean_advantage': advantages.mean().item(),
            'mean_return': returns.mean().item()
        }
    
    def save(self, path: str):
        """Save model checkpoint"""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        torch.save({
            'network_state_dict': self.network.state_dict(),
            'optimizer_state_dict': self.optimizer.state_dict(),
            'training_step': self.training_step
        }, path)
    
    def load(self, path: str):
        """Load model checkpoint"""
        # weights_only=True: model files are auto-discovered from a directory; a
        # poisoned .pt must not be able to execute arbitrary code on load. We only
        # read plain state_dict tensors + an int below, so this is safe.
        checkpoint = torch.load(path, map_location=self.device, weights_only=True)
        self.network.load_state_dict(checkpoint['network_state_dict'])
        self.optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        self.training_step = checkpoint.get('training_step', 0)
