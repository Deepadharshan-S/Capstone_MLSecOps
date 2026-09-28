import React, { createContext, useContext, useState, useCallback } from 'react';
import { datasetApi } from '../api/datasets';
import toast from 'react-hot-toast';

const DatasetContext = createContext();

export const useDatasets = () => useContext(DatasetContext);

export const DatasetProvider = ({ children }) => {
  const [datasets, setDatasets] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const fetchDatasets = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await datasetApi.getDatasets();
      setDatasets(data);
    } catch (err) {
      const errMsg = err.response?.data?.detail || 'Failed to fetch datasets';
      setError(errMsg);
      toast.error(errMsg);
    } finally {
      setLoading(false);
    }
  }, []);

  const uploadDataset = async (datasetName, file, description) => {
    try {
      toast.loading(`Uploading dataset ${datasetName}...`, { id: 'upload' });
      await datasetApi.uploadDataset(datasetName, file, description);
      toast.success(`Dataset ${datasetName} uploaded successfully!`, { id: 'upload' });
      await fetchDatasets(); // Refresh list
      return true;
    } catch (err) {
      // THIS IS WHERE WE CATCH SECURITY ALERT RESPONSES (400 Bad Request)
      const detail = err.response?.data?.detail;
      let errMsg = 'Failed to upload dataset';
      
      if (typeof detail === 'string') {
        errMsg = detail;
      } else if (Array.isArray(detail)) {
        // FastAPI sometimes returns array for validation errors
        errMsg = detail[0]?.msg || errMsg;
      }

      // Show security alert in a prominent toast
      toast.error(`Security Alert: ${errMsg}`, { 
        id: 'upload', 
        duration: 6000,
        style: { border: '1px solid #ef4444', backgroundColor: '#fee2e2', color: '#991b1b' }
      });
      
      throw err;
    }
  };

  const deleteDataset = async (datasetName) => {
    try {
      toast.loading(`Deleting dataset ${datasetName}...`, { id: 'delete' });
      await datasetApi.deleteDataset(datasetName);
      toast.success(`Dataset ${datasetName} deleted securely.`, { id: 'delete' });
      await fetchDatasets(); // Refresh list
      return true;
    } catch (err) {
      const errMsg = err.response?.data?.detail || 'Failed to delete dataset';
      toast.error(errMsg, { id: 'delete' });
      throw err;
    }
  };

  const value = {
    datasets,
    loading,
    error,
    fetchDatasets,
    uploadDataset,
    deleteDataset
  };

  return (
    <DatasetContext.Provider value={value}>
      {children}
    </DatasetContext.Provider>
  );
};
