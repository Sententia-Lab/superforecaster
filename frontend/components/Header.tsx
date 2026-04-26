"use client";

import AppBar from "@mui/material/AppBar";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Container from "@mui/material/Container";
import Toolbar from "@mui/material/Toolbar";
import Typography from "@mui/material/Typography";
import Link from "next/link";
import { usePathname } from "next/navigation";

const NAV_ITEMS = [
  { href: "/", label: "Submit & Vote" },
  { href: "/predictions", label: "In Progress" },
  { href: "/resolved", label: "Resolved" },
  { href: "/admin", label: "Admin" },
];

export function Header() {
  const pathname = usePathname();
  return (
    <AppBar position="sticky" color="default" elevation={0} sx={{ borderBottom: "1px solid", borderColor: "divider" }}>
      <Container maxWidth="lg">
        <Toolbar disableGutters sx={{ gap: 2, flexWrap: "wrap" }}>
          <Typography
            component={Link}
            href="/"
            variant="h3"
            sx={{
              flexShrink: 0,
              color: "text.primary",
              textDecoration: "none",
              fontWeight: 700,
            }}
          >
            Superforecaster
          </Typography>
          <Box sx={{ flexGrow: 1, display: "flex", gap: 1, ml: 2 }}>
            {NAV_ITEMS.map((item) => {
              const active =
                item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
              return (
                <Button
                  key={item.href}
                  component={Link}
                  href={item.href}
                  variant={active ? "contained" : "text"}
                  color={active ? "primary" : "inherit"}
                  size="small"
                >
                  {item.label}
                </Button>
              );
            })}
          </Box>
        </Toolbar>
      </Container>
    </AppBar>
  );
}
