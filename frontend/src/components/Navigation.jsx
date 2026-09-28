import React from 'react';
import { NavLink } from 'react-router-dom';
import { Database, Activity, LayoutDashboard, ShieldAlert } from 'lucide-react';
import { useAuth } from '../context/AuthContext';
import './Navigation.css';

const Navigation = () => {
  const { user, logout } = useAuth();

  return (
    <nav className="top-navigation glass-panel">
      <div className="nav-brand">
        <LayoutDashboard className="text-primary" />
        <h1>MLSecOps</h1>
      </div>
      
      <div className="nav-links">
        <NavLink 
          to="/" 
          className={({ isActive }) => isActive ? 'nav-item active' : 'nav-item'}
          end
        >
          <Database size={18} />
          <span>DataOps (Datasets)</span>
        </NavLink>
        
        <NavLink 
          to="/mlops" 
          className={({ isActive }) => isActive ? 'nav-item active' : 'nav-item'}
        >
          <Activity size={18} />
          <span>MLOps (Training)</span>
        </NavLink>

        {user?.role === 'admin' && (
          <NavLink 
            to="/audit" 
            className={({ isActive }) => isActive ? 'nav-item active' : 'nav-item'}
          >
            <ShieldAlert size={18} className="text-danger" />
            <span>Audit (Admin)</span>
          </NavLink>
        )}
      </div>

      <div className="nav-actions">
        <div className="user-badge">
          <span className="user-role">{user?.role}</span>
          <span className="user-name">{user?.username}</span>
        </div>
        <button className="btn btn-outline btn-sm" onClick={logout}>Logout</button>
      </div>
    </nav>
  );
};

export default Navigation;
