import type { Metadata } from 'next'
import './globals.css'

export const metadata: Metadata = {
  title: 'HKEX Dividend Monitor',
  description: 'Watch HKEX filings for dividend announcements',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-gray-950 text-gray-100 font-sans antialiased">
        <nav className="border-b border-gray-800 px-6 py-3 flex items-center gap-6">
          <span className="font-semibold text-brand-500 tracking-tight">HKEX Monitor</span>
          <a href="/"      className="text-sm text-gray-400 hover:text-white">Dashboard</a>
          <a href="/chat"  className="text-sm text-gray-400 hover:text-white">Chat</a>
        </nav>
        <main className="mx-auto max-w-6xl px-6 py-8">
          {children}
        </main>
      </body>
    </html>
  )
}
