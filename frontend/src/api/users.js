import api from './axios';

export const usersApi = {
  getAuditLogs: async () => {
    const response = await api.get('/users/audit-logs');
    return response.data; // List of AuditLogResponse
  }
};
