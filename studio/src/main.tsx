import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { CoreProvider } from "./api/CoreProvider";
import { takeTokenFromUrl } from "./api/token";
import "./styles/tokens.css";
import "./styles/app.css";

takeTokenFromUrl();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <CoreProvider>
      <App />
    </CoreProvider>
  </StrictMode>,
);
