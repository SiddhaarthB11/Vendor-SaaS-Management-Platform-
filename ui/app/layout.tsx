import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Derisk360 SLMCT",
  description: "Subscription, licences, and cost tracking for Derisk360 group operations.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
