"use client";

import { createTheme } from "@mui/material/styles";

export const theme = createTheme({
  cssVariables: {
    colorSchemeSelector: "data",
  },
  colorSchemes: {
    light: {
      palette: {
        primary: { main: "#0b6bcb" },
        secondary: { main: "#9c27b0" },
        background: { default: "#fafafa" },
      },
    },
    dark: {
      palette: {
        primary: { main: "#5eb0ff" },
        secondary: { main: "#ce93d8" },
        background: { default: "#0a0a0a", paper: "#121212" },
      },
    },
  },
  typography: {
    fontFamily:
      'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
    h1: { fontSize: "2rem", fontWeight: 700 },
    h2: { fontSize: "1.5rem", fontWeight: 700 },
    h3: { fontSize: "1.25rem", fontWeight: 600 },
  },
  shape: { borderRadius: 8 },
  components: {
    MuiCard: {
      styleOverrides: {
        root: { transition: "border-color 0.15s ease" },
      },
    },
    MuiContainer: {
      defaultProps: { maxWidth: "md" },
    },
  },
});
