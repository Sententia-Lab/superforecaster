"use client";

import {
  Alert,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  TextField,
} from "@mui/material";
import { useState } from "react";

import { setAdminToken } from "../lib/api";

export function AdminLogin({ onSubmit }: { onSubmit: () => void }) {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);

  function save() {
    if (!token.trim()) {
      setError("Token cannot be empty.");
      return;
    }
    setAdminToken(token.trim());
    onSubmit();
  }

  return (
    <Dialog open onClose={() => {}} fullWidth maxWidth="xs">
      <DialogTitle>Admin access</DialogTitle>
      <DialogContent>
        <DialogContentText sx={{ mb: 2 }}>
          Enter the <code>ADMIN_API_KEY</code> from your backend <code>.env</code>. The token is
          stored in your browser&apos;s localStorage and sent as a Bearer token on admin
          requests.
        </DialogContentText>
        {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
        <TextField
          autoFocus
          fullWidth
          label="Admin token"
          type="password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && save()}
        />
      </DialogContent>
      <DialogActions>
        <Button onClick={save} variant="contained">Save</Button>
      </DialogActions>
    </Dialog>
  );
}
