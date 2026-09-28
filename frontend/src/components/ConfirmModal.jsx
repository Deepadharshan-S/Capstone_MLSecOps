import React, { useState } from 'react';
import { AlertTriangle, X } from 'lucide-react';
import './ConfirmModal.css';

const ConfirmModal = ({ isOpen, onClose, onConfirm, datasetName }) => {
  const [inputValue, setInputValue] = useState('');

  if (!isOpen) return null;

  const handleConfirm = () => {
    if (inputValue === datasetName) {
      onConfirm();
      setInputValue(''); // Reset on close
    }
  };

  return (
    <div className="modal-overlay flex-center animate-fade-in">
      <div className="modal-card glass-panel">
        <button className="modal-close-btn" onClick={onClose}>
          <X size={20} />
        </button>
        
        <div className="modal-header">
          <div className="modal-icon-container">
            <AlertTriangle className="modal-icon" size={28} />
          </div>
          <h2>Delete Dataset</h2>
        </div>
        
        <div className="modal-body">
          <p>
            You are about to securely delete <strong>{datasetName}</strong>. 
            This action will permanently erase the repository and all version control history. 
            This cannot be undone.
          </p>
          
          <div className="modal-confirm-section">
            <label htmlFor="confirm-input">
              Please type <strong>{datasetName}</strong> to confirm:
            </label>
            <input 
              id="confirm-input"
              type="text" 
              className="form-input"
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              placeholder={datasetName}
              autoComplete="off"
            />
          </div>
        </div>
        
        <div className="modal-footer">
          <button className="btn btn-outline" onClick={onClose}>Cancel</button>
          <button 
            className="btn btn-danger" 
            onClick={handleConfirm}
            disabled={inputValue !== datasetName}
          >
            Permanently Delete
          </button>
        </div>
      </div>
    </div>
  );
};

export default ConfirmModal;
