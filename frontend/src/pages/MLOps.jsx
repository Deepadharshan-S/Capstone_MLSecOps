import React, { useEffect, useState } from 'react';
import { useAuth } from '../context/AuthContext';
import { useMLOps } from '../context/MLOpsContext';
import { useDatasets } from '../context/DatasetContext';
import { Activity, Play, Box, Server } from 'lucide-react';
import './MLOps.css';

const MLOps = () => {
  const { user, logout } = useAuth();
  const { models, deployments, loading, fetchModels, fetchDeployments, trainPipeline } = useMLOps();
  const { datasets, fetchDatasets } = useDatasets();
  
  const [isTrainModalOpen, setIsTrainModalOpen] = useState(false);
  const [selectedDataset, setSelectedDataset] = useState('');
  const [targetColumn, setTargetColumn] = useState('');
  const [isTraining, setIsTraining] = useState(false);

  useEffect(() => {
    fetchModels();
    fetchDeployments();
    fetchDatasets(); // We need datasets to select from in the dropdown
  }, [fetchModels, fetchDeployments, fetchDatasets]);

  const handleTrainSubmit = async (e) => {
    e.preventDefault();
    if (!selectedDataset || !targetColumn) return;
    
    setIsTraining(true);
    try {
      await trainPipeline(selectedDataset, targetColumn);
      setIsTrainModalOpen(false);
      setSelectedDataset('');
      setTargetColumn('');
    } catch (err) {
      // Handled by context toast
    } finally {
      setIsTraining(false);
    }
  };

  return (
    <div className="mlops-layout">
      <main className="dashboard-content">
        <div className="content-header">
          <h2>Model Registry</h2>
          <button 
            className="btn btn-primary"
            onClick={() => setIsTrainModalOpen(true)}
          >
            <Play size={18} />
            <span>Train New Model</span>
          </button>
        </div>

        {/* Models Grid */}
        <div className="dataset-grid">
          {loading && models.length === 0 ? (
            <div className="loading-state flex-center">Loading models...</div>
          ) : models.length === 0 ? (
            <div className="empty-state flex-center glass-panel">
              <Box size={48} className="text-muted" />
              <h3>No Models Found</h3>
              <p>Train your first model using an uploaded dataset.</p>
            </div>
          ) : (
            models.map((model) => (
              <div key={model.id} className="dataset-card glass-panel animate-fade-in">
                <div className="dataset-card-header">
                  <h3 className="dataset-name">{model.name}</h3>
                  <span className="badge badge-primary">
                    v{model.version || '1'}
                  </span>
                </div>
                <p className="dataset-meta">
                  Experiment: <span>{model.experiment_name || 'N/A'}</span>
                </p>
                <div className="dataset-card-footer">
                  <button className="btn btn-outline btn-sm">
                    <Server size={14} /> Deploy
                  </button>
                </div>
              </div>
            ))
          )}
        </div>
      </main>

      {/* Train Modal */}
      {isTrainModalOpen && (
        <div className="modal-overlay flex-center animate-fade-in">
          <div className="modal-card glass-panel">
            <div className="modal-header">
              <h2>Automated Pipeline Training</h2>
            </div>
            <form onSubmit={handleTrainSubmit} className="upload-form">
              <div className="form-group">
                <label className="form-label">Select Dataset</label>
                <select 
                  className="form-input" 
                  value={selectedDataset}
                  onChange={(e) => setSelectedDataset(e.target.value)}
                  required
                >
                  <option value="" disabled>-- Select a Dataset --</option>
                  {datasets.map(d => (
                    <option key={d.id} value={d.name}>{d.name}</option>
                  ))}
                </select>
              </div>
              <div className="form-group">
                <label className="form-label">Target Column (to predict)</label>
                <input 
                  type="text" 
                  className="form-input" 
                  value={targetColumn}
                  onChange={(e) => setTargetColumn(e.target.value)}
                  placeholder="e.g., is_fraud"
                  required
                />
              </div>
              <div className="modal-footer">
                <button type="button" className="btn btn-outline" onClick={() => setIsTrainModalOpen(false)}>Cancel</button>
                <button type="submit" className="btn btn-primary" disabled={isTraining || !selectedDataset || !targetColumn}>
                  {isTraining ? 'Initializing...' : 'Start Training'}
                  {!isTraining && <Play size={16} />}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
};

export default MLOps;
