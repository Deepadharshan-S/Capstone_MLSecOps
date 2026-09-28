import React from 'react';
import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom';
import { Toaster } from 'react-hot-toast';
import { AuthProvider, useAuth } from './context/AuthContext';
import { DatasetProvider } from './context/DatasetContext';
import { MLOpsProvider } from './context/MLOpsContext';
import Navigation from './components/Navigation';
import Login from './pages/Login';
import Dashboard from './pages/Dashboard';
import MLOps from './pages/MLOps';
import AuditLogs from './pages/AuditLogs';
import './index.css';

// A simple protected route wrapper
const ProtectedRoute = ({ children }) => {
  const { user, loading } = useAuth();
  
  if (loading) {
    return <div className="flex-center" style={{ height: '100vh' }}>Loading...</div>;
  }
  
  if (!user) {
    return <Navigate to="/login" replace />;
  }
  
  return (
    <>
      <Navigation />
      {children}
    </>
  );
};

function App() {
  return (
    <AuthProvider>
      <DatasetProvider>
        <MLOpsProvider>
          <Router>
            <Toaster position="top-right" />
            <Routes>
              <Route path="/login" element={<Login />} />
              
              {/* Protected Routes */}
              <Route 
                path="/" 
                element={
                  <ProtectedRoute>
                    <Dashboard />
                  </ProtectedRoute>
                } 
              />
              <Route 
                path="/mlops" 
                element={
                  <ProtectedRoute>
                    <MLOps />
                  </ProtectedRoute>
                } 
              />
              <Route 
                path="/audit" 
                element={
                  <ProtectedRoute>
                    <AuditLogs />
                  </ProtectedRoute>
                } 
              />
              
              {/* Fallback */}
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </Router>
        </MLOpsProvider>
      </DatasetProvider>
    </AuthProvider>
  );
}

export default App;
