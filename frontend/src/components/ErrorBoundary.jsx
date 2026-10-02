import { Component } from 'react'
import { AlertTriangle, RefreshCw, RotateCcw } from './icons.jsx'
import { Button } from './ui/index.jsx'

/**
 * ErrorBoundary — keeps one bad page from blanking the whole shell.
 * Wraps the routed <App />; a render crash falls back to a recoverable card.
 */
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { error: null, key: 0 }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
    // Surfaced in the console for debugging; the UI handles presentation.
    console.error('[MLSecOps] render error', error, info)
  }

  handleRetry = () => {
    this.setState((s) => ({ error: null, key: s.key + 1 }))
  }

  handleHome = () => {
    this.setState({ error: null, key: this.state.key + 1 })
    window.location.assign('/')
  }

  render() {
    if (!this.state.error) {
      return <div key={this.state.key}>{this.props.children}</div>
    }
    const msg = this.state.error?.message || String(this.state.error)
    return (
      <div className="page">
        <div className="panel" style={{ maxWidth: 720, margin: '0 auto' }}>
          <div className="panel-body">
            <div className="empty-state">
              <div className="empty-state-icon" style={{ background: 'var(--danger-soft)', color: 'var(--danger)' }}>
                <AlertTriangle size={22} />
              </div>
              <div className="empty-state-title">This view crashed</div>
              <div className="empty-state-desc">
                Something in the UI threw while rendering. The backend is likely
                fine — retry the view, or go back to the dashboard.
              </div>
              <pre
                className="code-block"
                style={{ textAlign: 'left', maxWidth: 560, marginTop: 14, fontSize: 12 }}
              >
                {msg}
              </pre>
              <div className="empty-state-actions">
                <Button variant="primary" icon={RefreshCw} onClick={this.handleRetry}>
                  Retry view
                </Button>
                <Button variant="secondary" icon={RotateCcw} onClick={this.handleHome}>
                  Back to dashboard
                </Button>
              </div>
            </div>
          </div>
        </div>
      </div>
    )
  }
}
