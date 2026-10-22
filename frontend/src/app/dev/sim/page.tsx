import type { Metadata } from "next";
import { SimDemo } from "./SimDemo";

export const metadata: Metadata = {
  title: "Moonwright · sim dev",
  description: "Development page for the 2D replay, simulation controls, event list and before/after view.",
};

export default function SimDevPage() {
  return <SimDemo />;
}
