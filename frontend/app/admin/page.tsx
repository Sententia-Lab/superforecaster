"use client";

import {
  Alert,
  Box,
  Button,
  Container,
  Snackbar,
  Stack,
  Tab,
  Tabs,
  Typography,
} from "@mui/material";
import { useEffect, useState } from "react";

import { AdminLogin } from "../../components/AdminLogin";
import { ForecastsAdminTab } from "./ForecastsAdminTab";
import { MonthlyTopTab } from "./MonthlyTopTab";
import { PendingQuestionsTab } from "./PendingQuestionsTab";
import { ApprovedQuestionsTab } from "./ApprovedQuestionsTab";
import { getAdminToken, setAdminToken } from "../../lib/api";

type TabKey = "pending" | "approved" | "monthly" | "forecasts";

export default function AdminPage() {
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [tab, setTab] = useState<TabKey>("pending");
  const [toast, setToast] = useState<{ msg: string; severity: "success" | "error" } | null>(null);

  useEffect(() => {
    setAuthed(getAdminToken() !== null);
  }, []);

  function notify(msg: string, severity: "success" | "error" = "success") {
    setToast({ msg, severity });
  }

  if (authed === null) return null;
  if (!authed) {
    return <AdminLogin onSubmit={() => setAuthed(true)} />;
  }

  return (
    <Container maxWidth="lg" sx={{ py: 4 }}>
      <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 3 }}>
        <Typography variant="h1">Admin</Typography>
        <Button
          size="small"
          onClick={() => {
            setAdminToken(null);
            setAuthed(false);
          }}
        >
          Sign out
        </Button>
      </Stack>

      <Box sx={{ borderBottom: 1, borderColor: "divider", mb: 3 }}>
        <Tabs value={tab} onChange={(_, v) => setTab(v)}>
          <Tab value="pending" label="Pending Questions" />
          <Tab value="approved" label="Approved Questions" />
          <Tab value="monthly" label="Monthly Top" />
          <Tab value="forecasts" label="Forecasts" />
        </Tabs>
      </Box>

      {tab === "pending" && <PendingQuestionsTab notify={notify} />}
      {tab === "approved" && <ApprovedQuestionsTab notify={notify} />}
      {tab === "monthly" && <MonthlyTopTab notify={notify} />}
      {tab === "forecasts" && <ForecastsAdminTab notify={notify} />}

      <Snackbar
        open={toast !== null}
        autoHideDuration={5000}
        onClose={() => setToast(null)}
        anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
      >
        {toast ? (
          <Alert
            severity={toast.severity}
            onClose={() => setToast(null)}
            sx={{ width: "100%" }}
          >
            {toast.msg}
          </Alert>
        ) : (
          <span />
        )}
      </Snackbar>
    </Container>
  );
}
