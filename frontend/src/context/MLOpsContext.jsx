import React, { createContext, useContext, useState, useCallback } from 'react';
import { mlopsApi } from '../api/mlops';
import toast from 'react-hot-toast';

const MLOpsContext = createContext();

export const useMLOps = () => useContext(MLOpsContext);

export const MLOpsProvider = ({ children }) => {
  const [models, setModels] = useState([]);
  const [deployments, setDeployments] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const fetchModels = useCallback(async () => {
    setLoading(true);
    try {
      const data = await mlopsApi.getModels();
      setModels(data);
    } catch (err) {
      const errMsg = err.response?.data?.detail || 'Failed to fetch models';
      setError(errMsg);
      toast.error(errMsg);
    } finally {
      setLoading(false);
    }
  }, []);

  const fetchDeployments = useCallback(async () => {
    try {
      const data = await mlopsApi.getDeployments();
      setDeployments(data);
    } catch (err) {
      console.error('Failed to fetch deployments:', err);
    }
  }, []);

  const trainPipeline = async (datasetId, targetColumn, modelType = 'RandomForest', experimentName = 'default-experiment') => {
    try {
      toast.loading(`Initializing pipeline training for ${datasetId}...`, { id: 'train' });
      const payload = {
        dataset_id: datasetId,
        ref: 'main',
        target_column: targetColumn,
        model_type: modelType,
        experiment_name: experimentName,
        hyperparameters: {}
      };
      
      const response = await mlopsApi.trainPipeline(payload);
      toast.success(`Training Job ${response.job_id || 'started'} successfully!`, { id: 'train' });
      await fetchModels(); // Refresh list to see new model if it completes fast (or just logs)
      return response;
    } catch (err) {
      const errMsg = err.response?.data?.detail || 'Training failed';
      toast.error(errMsg, { id: 'train' });
      throw err;
    }
  };

  const value = {
    models,
    deployments,
    loading,
    error,
    fetchModels,
    fetchDeployments,
    trainPipeline
  };

  return (
    <MLOpsContext.Provider value={value}>
      {children}
    </MLOpsContext.Provider>
  );
};
