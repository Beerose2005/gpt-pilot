import { BrowserRouter as Router, Routes, Route } from "react-router-dom"
import { ThemeProvider } from "./components/ui/theme-provider"
import { Toaster } from "./components/ui/toaster"
import { AuthProvider } from "./contexts/AuthContext"
{% if options.auth %}
import { Login } from "./pages/Login"
import { Register } from "./pages/Register"
import { ProtectedRoute } from "./components/ProtectedRoute"
{% endif %}
import { Layout } from "./components/Layout"
import { BlankPage } from "./pages/BlankPage"

function App() {
  return (
    <AuthProvider>
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
    </AuthProvider>
  )
}

export default App
