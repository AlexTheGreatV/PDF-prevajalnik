import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "JUB/KOOP PDF Pipeline",
  description: "Internal document translation PDF pipeline",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="sl">
      <body>{children}</body>
    </html>
  );
}

