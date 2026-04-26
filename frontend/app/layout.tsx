import type { Metadata } from "next";

import { Header } from "../components/Header";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "Superforecaster",
  description:
    "Crowd-sourced superforecasting platform powered by Tetlock's methodology.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body style={{ margin: 0 }}>
        <Providers>
          <Header />
          {children}
        </Providers>
      </body>
    </html>
  );
}
