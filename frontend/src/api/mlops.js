import api from './axios';

export const mlopsApi = {
  // Get all registered models
  getModels: async () => {
    const response = await api.get('/models');
    return response.data.models || [];
  },

  // Get all active deployments
  getDeployments: async () => {
    const response = await api.get('/deployments?active_only=false');
    return response.data.deployments || [];
  },

  // Train a model using the automated pipeline
  trainPipeline: async (payload) => {
    // payload should contain: dataset_id, target_column, model_type, etc.
    const response = await api.post('/models/train-pipeline', payload);
    return response.data;
  },

  // Deploy a model
  deployModel: async (payload) => {
    // payload should contain: model_id, environment, version, replicas
    const response = await api.post('/models/deploy', payload);
    return response.data;
  }
};
