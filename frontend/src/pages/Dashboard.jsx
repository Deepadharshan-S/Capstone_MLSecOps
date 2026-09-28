import React, { useEffect, useState, useRef } from 'react';
import { useAuth } from '../context/AuthContext';
import { useDatasets } from '../context/DatasetContext';
import { datasetApi } from '../api/datasets';
import ConfirmModal from '../components/ConfirmModal';
import { Database, Upload, Trash2, Search, Plus } from 'lucide-react';
import './Dashboard.css';

const Dashboard = () => {
  const { user, logout } = useAuth();
  const { datasets, loading, fetchDatasets, uploadDataset, deleteDataset } = useDatasets();
  
  // Local state for modals and forms
  const [isDeleteModalOpen, setIsDeleteModalOpen] = useState(false);
  const [datasetToDelete, setDatasetToDelete] = useState(null);
  
  const [isUploadModalOpen, setIsUploadModalOpen] = useState(false);
  const [uploadName, setUploadName] = useState('');
  const [uploadFile, setUploadFile] = useState(null);
  const [uploadDescription, setUploadDescription] = useState('');
  const [isUploading, setIsUploading] = useState(false);
  
  const [isViewModalOpen, setIsViewModalOpen] = useState(false);
  const [viewData, setViewData] = useState('');
  const [viewFileName, setViewFileName] = useState('');
  
  const fileInputRef = useRef(null);

  useEffect(() => {
    fetchDatasets();
  }, [fetchDatasets]);

  const handleDeleteClick = (datasetName) => {
    setDatasetToDelete(datasetName);
    setIsDeleteModalOpen(true);
  };

  const handleConfirmDelete = async () => {
    if (datasetToDelete) {
      await deleteDataset(datasetToDelete);
      setIsDeleteModalOpen(false);
      setDatasetToDelete(null);
    }
  };

  const handleFileChange = (e) => {
    if (e.target.files && e.target.files[0]) {
      setUploadFile(e.target.files[0]);
    }
  };

  const handleDownloadClick = async (datasetName, metadata) => {
    let fileName = metadata?.filename;
    if (!fileName) {
      fileName = prompt(`Enter the filename you want to view from dataset '${datasetName}' (e.g., init.csv):`);
    }
    if (!fileName) return;

    try {
      const data = await datasetApi.fetchDatasetContent(datasetName, fileName);
      setViewData(data);
      setViewFileName(fileName);
      setIsViewModalOpen(true);
    } catch (err) {
      alert(`Failed to fetch ${fileName}. Are you sure that's the correct name?`);
    }
  };

  const handleUploadSubmit = async (e) => {
    e.preventDefault();
    if (!uploadFile || !uploadName) return;
    
    setIsUploading(true);
    try {
      await uploadDataset(uploadName, uploadFile, uploadDescription);
      // Reset form and close on success
      setIsUploadModalOpen(false);
      setUploadName('');
      setUploadFile(null);
      setUploadDescription('');
      if (fileInputRef.current) fileInputRef.current.value = '';
    } catch (err) {
      // Error is handled by context toast, just stop uploading state
    } finally {
      setIsUploading(false);
    }
  };

  return (
    <div className="dashboard-layout">
      <main className="dashboard-content">
        <div className="content-header">
          <h2>Dataset Registry</h2>
          <button 
            className="btn btn-primary"
            onClick={() => setIsUploadModalOpen(true)}
          >
            <Plus size={18} />
            <span>New Dataset</span>
          </button>
        </div>

        {/* Dataset Grid */}
        <div className="dataset-grid">
          {loading && datasets.length === 0 ? (
            <div className="loading-state flex-center">Loading datasets...</div>
          ) : datasets.length === 0 ? (
            <div className="empty-state flex-center glass-panel">
              <Database size={48} className="text-muted" />
              <h3>No Datasets Found</h3>
              <p>Upload your first dataset to get started.</p>
            </div>
          ) : (
            datasets.map((dataset) => (
              <div key={dataset.id} className="dataset-card glass-panel animate-fade-in">
                <div className="dataset-card-header">
                  <h3 className="dataset-name">{dataset.name}</h3>
                  <span className="badge badge-primary">
                    {dataset.default_branch || 'main'}
                  </span>
                </div>
                <p className="dataset-meta">
                  Created by: <span>{dataset.metadata_info?.author || 'Unknown'}</span>
                </p>
                <div className="dataset-card-footer">
                  <button 
                    className="btn btn-outline btn-sm"
                    onClick={() => handleDownloadClick(dataset.name, dataset.metadata_info)}
                  >
                    <Search size={14} /> View
                  </button>
                  <button 
                    className="btn btn-danger btn-sm"
                    onClick={() => handleDeleteClick(dataset.name)}
                  >
                    <Trash2 size={14} /> Delete
                  </button>
                </div>
              </div>
            ))
          )}
        </div>
      </main>

      {/* Delete Confirmation Modal */}
      <ConfirmModal 
        isOpen={isDeleteModalOpen}
        onClose={() => setIsDeleteModalOpen(false)}
        onConfirm={handleConfirmDelete}
        datasetName={datasetToDelete}
      />

      {/* Upload Modal (Inline for simplicity) */}
      {isUploadModalOpen && (
        <div className="modal-overlay flex-center animate-fade-in">
          <div className="modal-card glass-panel">
            <div className="modal-header">
              <h2>Upload Dataset</h2>
            </div>
            <form onSubmit={handleUploadSubmit} className="upload-form">
              <div className="form-group">
                <label className="form-label">Dataset Name</label>
                <input 
                  type="text" 
                  className="form-input" 
                  value={uploadName}
                  onChange={(e) => setUploadName(e.target.value)}
                  placeholder="e.g., fraud-detection-v1"
                  required
                />
              </div>
              <div className="form-group">
                <label className="form-label">Description (Optional)</label>
                <input 
                  type="text" 
                  className="form-input" 
                  value={uploadDescription}
                  onChange={(e) => setUploadDescription(e.target.value)}
                  placeholder="What is this data used for?"
                />
              </div>
              <div className="form-group file-upload-group">
                <label className="form-label">Data File (CSV/JSON)</label>
                <input 
                  type="file" 
                  className="form-input file-input" 
                  onChange={handleFileChange}
                  ref={fileInputRef}
                  required
                />
              </div>
              <div className="modal-footer">
                <button type="button" className="btn btn-outline" onClick={() => setIsUploadModalOpen(false)}>Cancel</button>
                <button type="submit" className="btn btn-primary" disabled={isUploading || !uploadFile || !uploadName}>
                  {isUploading ? 'Uploading & Scanning...' : 'Secure Upload'}
                  {!isUploading && <Upload size={16} />}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* View Modal */}
      {isViewModalOpen && (
        <div className="modal-overlay flex-center animate-fade-in" style={{ zIndex: 1000 }}>
          <div className="modal-card glass-panel" style={{ width: '80%', maxWidth: '800px', maxHeight: '80vh', display: 'flex', flexDirection: 'column' }}>
            <div className="modal-header">
              <h2>Viewing: {viewFileName}</h2>
            </div>
            <div className="modal-body" style={{ flex: 1, overflow: 'auto', padding: '1rem', backgroundColor: 'var(--color-bg-dark)', borderRadius: 'var(--radius-md)' }}>
              <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordWrap: 'break-word', color: 'var(--color-text-secondary)', fontSize: '0.875rem' }}>
                {viewData}
              </pre>
            </div>
            <div className="modal-footer" style={{ marginTop: '1rem' }}>
              <button className="btn btn-primary" onClick={() => setIsViewModalOpen(false)}>Close</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default Dashboard;
