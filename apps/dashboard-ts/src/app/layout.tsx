import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "ZBM Revenue Recovery",
  description: "Revenue Recovery 1A — detection findings (non-live fixture data)",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body style={{ margin: 0, fontFamily: "system-ui, sans-serif", background: "#0b0d10", color: "#e8eaed" }}>
        {children}
      </body>
    </html>
  );
}
