import { Component } from "react";

export class ErrorBoundary extends Component {
  state = { error: false };
  static getDerivedStateFromError() {
    return { error: true };
  }
  render() {
    return this.state.error ? (
      <div className="empty">
        <h1>Dashboard could not render</h1>
        <p>Your saved scans are unchanged.</p>
        <button className="primary" onClick={() => location.reload()}>
          Reload dashboard
        </button>
      </div>
    ) : (
      this.props.children
    );
  }
}
