import api from './axios';

export const datasetApi = {
  // Fetch all datasets
  getDatasets: async () => {
    const response = await api.get('/datasets/');
    return response.data;
  },

  // Upload a new dataset (handles multipart/form-data)
  uploadDataset: async (datasetName, file, description = '', classification = 'confidential') => {
    const formData = new FormData();
    formData.append('name', datasetName);
    formData.append('description', description);
    formData.append('file', file);
    
    // Metadata can be customized based on requirements
    const metadata = {
      description,
      classification
    };
    
    // The backend register endpoint expects name, description, and file. 
    // It doesn't natively take a 'metadata' string in the form data for register, but we can pass it if we want.
    // For now we rely on the core fields.
    const response = await api.post(`/datasets`, formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
    });
    
    return response.data;
  },

  // Delete an entire dataset
  deleteDataset: async (datasetName) => {
    const response = await api.delete(`/datasets/${datasetName}`);
    return response.data;
  },

  // Download a dataset
  downloadDataset: async (datasetName, fileName) => {
    // Fetch as blob
    const response = await api.get(`/datasets/${datasetName}/download`, {
      params: { path: fileName },
      responseType: 'blob', // Important for downloading files
    });
    
    // Create a Blob from the Stream
    const blob = new Blob([response.data], { type: response.headers['content-type'] });
    const url = window.URL.createObjectURL(blob);
    
    const link = document.createElement('a');
    link.href = url;
    link.setAttribute('download', fileName);
    document.body.appendChild(link);
    link.click();
    link.parentNode.removeChild(link);
  },

  // Fetch dataset content for viewing inline
  fetchDatasetContent: async (datasetName, fileName) => {
    const response = await api.get(`/datasets/${datasetName}/download`, {
      params: { path: fileName },
      responseType: 'text',
    });
    return response.data;
  }
};
