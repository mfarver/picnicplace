import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { CoreProvider } from "./api/CoreProvider";
import { CameraWindow } from "./components/CameraWindow";
import { ErrorScreen } from "./components/ErrorScreen";
import { takeTokenFromUrl } from "./api/token";
import "./styles/tokens.css";
import "./styles/app.css";

takeTokenFromUrl();

// `?camera=ID`: a camera's own window, opened from a camera view's pop-out button.
const camera = new URLSearchParams(window.location.search).get("camera");

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ErrorScreen>
      <CoreProvider>
        {camera ? <CameraWindow id={camera} /> : <App />}
      </CoreProvider>
    </ErrorScreen>
  </StrictMode>,
);
