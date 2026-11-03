import "./globals.css";

export const metadata = { title: "TRACEFORGE", description: "Independent verification for AI-generated code" };

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
