import { Component, type ErrorInfo, type ReactNode } from "react";
import { ErrorPage, buttonClass } from "./ErrorPage";

interface State {
  error: Error | null;
}

/** Top-level boundary: a render crash shows a full-page error, not a blank page. */
export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  override state: State = { error: null };

  static getDerivedStateFromError(error: unknown): State {
    return { error: error instanceof Error ? error : new Error(String(error)) };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("Dashboard crashed", error, info.componentStack);
  }

  override render(): ReactNode {
    if (this.state.error) {
      return (
        <ErrorPage title="Something went wrong" message={this.state.error.message}>
          <button type="button" className={buttonClass} onClick={() => window.location.reload()}>
            Reload
          </button>
        </ErrorPage>
      );
    }
    return this.props.children;
  }
}
