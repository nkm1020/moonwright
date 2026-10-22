import { App } from "@/components/App";

// The whole console is client-side state (the backend is called from the browser), so the
// page is a static shell that renders the client component; no server data fetching here.
export default function Home() {
  return <App />;
}
