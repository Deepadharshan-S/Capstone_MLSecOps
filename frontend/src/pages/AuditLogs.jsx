import React, { useEffect, useState } from 'react';
import { useAuth } from '../context/AuthContext';
import { usersApi } from '../api/users';
import { ShieldAlert, AlertTriangle, CheckCircle, Activity, Info } from 'lucide-react';
import toast from 'react-hot-toast';
import './AuditLogs.css';

const AuditLogs = () => {
  const { user, logout } = useAuth();
  const [logs, setLogs] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fetchLogs = async () => {
      try {
        const data = await usersApi.getAuditLogs();
        setLogs(data);
      } catch (err) {
        const errMsg = err.response?.data?.detail || 'Failed to fetch audit logs';
        toast.error(errMsg);
      } finally {
        setLoading(false);
      }
    };
    fetchLogs();
  }, []);

  // Helper to format date
  const formatDate = (dateStr) => {
    return new Date(dateStr).toLocaleString();
  };

  // Helper to determine icon/color based on action type
  const getActionBadge = (action) => {
    if (action.includes('intercepted') || action.includes('error') || action.includes('rejected')) {
      return (
        <span className="badge badge-danger">
          <AlertTriangle size={12} /> {action}
        </span>
      );
    }
    if (action.includes('success') || action.includes('completed')) {
      return (
        <span className="badge badge-success">
          <CheckCircle size={12} /> {action}
        </span>
      );
    }
    if (action.includes('delete') || action.includes('removed')) {
      return (
        <span className="badge badge-warning">
          <Activity size={12} /> {action}
        </span>
      );
    }
    return (
      <span className="badge badge-primary">
        <Info size={12} /> {action}
      </span>
    );
  };

  return (
    <div className="audit-layout">
      <main className="dashboard-content">
        <div className="content-header">
          <h2>Immutable Event History</h2>
        </div>

        <div className="table-container glass-panel animate-fade-in">
          {loading ? (
            <div className="loading-state flex-center">Loading audit logs...</div>
          ) : logs.length === 0 ? (
            <div className="empty-state flex-center">
              <ShieldAlert size={48} className="text-muted" />
              <h3>No Logs Found</h3>
              <p>System audit trail is empty.</p>
            </div>
          ) : (
            <table className="audit-table">
              <thead>
                <tr>
                  <th>Timestamp</th>
                  <th>Username</th>
                  <th>Action</th>
                  <th>IP Address</th>
                  <th>Details</th>
                </tr>
              </thead>
              <tbody>
                {logs.map((log) => (
                  <tr key={log.id}>
                    <td className="timestamp-cell">{formatDate(log.timestamp)}</td>
                    <td className="username-cell">{log.username}</td>
                    <td>{getActionBadge(log.action)}</td>
                    <td className="ip-cell">{log.ip_address || 'Internal'}</td>
                    <td className="details-cell">{log.details}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </main>
    </div>
  );
};

export default AuditLogs;
