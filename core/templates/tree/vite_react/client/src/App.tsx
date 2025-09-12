import { BrowserRouter as Router, Routes, Route } from "react-router-dom"
import { ThemeProvider } from "./components/ui/theme-provider"
import { Toaster } from "./components/ui/toaster"
{% if options.auth %}
import { AuthProvider } from "./contexts/AuthContext"
import { Login } from "./pages/Login"
import { Register } from "./pages/Register"
import { ProtectedRoute } from "./components/ProtectedRoute"
{% endif %}
import { Layout } from "./components/Layout"
import { BlankPage } from "./pages/BlankPage"

function App() {
  return (
{% if options.auth %}
  <AuthProvider>
{% endif %}
    <ThemeProvider defaultTheme="light" storageKey="ui-theme">
      <Router>
        <Routes>
{% if options.auth %}
          <Route path="/login" element={<Login />} />
          <Route path="/register" element={<Register />} />
          <Route path="/" element={<ProtectedRoute> <Layout /> </ProtectedRoute>} />
{% else %}
          <Route path="/" element={<Layout />} />
          {% endif %}
          <Route path="*" element={<BlankPage />} />
        </Routes>
      </Router>
      <Toaster />
    </ThemeProvider>
{% if options.auth %}
  </AuthProvider>
{% endif %}
  )
}

export default App
