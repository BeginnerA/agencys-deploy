import re
import unittest
from deploy_strategies.tomcat_config import (config_digest, parse_war_config,
                                            render_instance_files, render_war_unit,
                                            validate_war_config)

CFG = parse_war_config({
    "app_home": "/opt/apps/orders", "tomcat_home": "/opt/tomcat",
    "java_home": "/usr/lib/jvm/java-17", "run_user": "appuser",
    "unit_name": "orders-war", "port": 18080, "context_path": "/orders",
    "jvm_opts": ["-Xms256m"], "env_files": ["/etc/smartops/orders.env"],
    "readiness": {"path": "/health"},
})
TEMPLATES = {"web.xml": "<web-app/>", "catalina.properties": "a=1\n",
             "logging.properties": "handlers=x\n"}

class RenderTest(unittest.TestCase):
    def test_server_xml_has_single_http_connector_and_safe_host(self):
        files = render_instance_files(CFG, "orders", TEMPLATES)
        server = files["conf/server.xml"]
        self.assertIn('port="18080"', server)
        self.assertIn('address="0.0.0.0"', server)
        self.assertIn('autoDeploy="false"', server)
        self.assertIn('deployOnStartup="true"', server)
        # release 目录只读，且外部 WAR 的展开副本会落在 CURRENT/ 旁；直接读 WAR，
        # 不产生会过期的展开副本（instance/webapps 无需清理）。
        self.assertIn('unpackWARs="false"', server)
        self.assertIn('<Server port="-1"', server)
        # 只按连接器写法断言：8080=默认 HTTP、8009=AJP、8005=默认 shutdown；
        # 避免子串误判（18080 本身含 "8080"）。
        for forbidden in ("AJP", "Manager", "HostManager",
                          'port="8080"', 'port="8009"', 'port="8005"'):
            self.assertNotIn(forbidden, server)

    def test_context_filename_and_docbase(self):
        files = render_instance_files(CFG, "orders", TEMPLATES)
        ctx = files["conf/Catalina/localhost/orders.xml"]
        self.assertIn('docBase="/opt/apps/orders/CURRENT/app.war"', ctx)
        self.assertIn('reloadable="false"', ctx)
        self.assertNotIn('path="', ctx)

    def test_templates_required(self):
        with self.assertRaises(ValueError):
            render_instance_files(CFG, "orders", {"web.xml": "<web-app/>"})

    def test_unit_direct_java_and_no_restart(self):
        unit = render_war_unit(CFG, "orders")
        self.assertIn("/usr/lib/jvm/java-17/bin/java", unit)
        self.assertIn("-Dcatalina.home=/opt/tomcat", unit)
        self.assertIn("-Dcatalina.base=/opt/apps/orders/instance", unit)
        self.assertIn("org.apache.catalina.startup.Bootstrap start", unit)
        self.assertIn("Restart=no", unit)
        self.assertIn("KillMode=control-group", unit)
        self.assertIn("User=appuser", unit)
        self.assertNotIn("catalina.sh", unit)

    def test_digest_changes_with_file_or_config(self):
        files = render_instance_files(CFG, "orders", TEMPLATES)
        a = config_digest(CFG, files, "tomcat-template-v1")
        b = config_digest(dict(CFG, port=18081), files, "tomcat-template-v1")
        c = config_digest(CFG, dict(files, **{"conf/web.xml": "<web-app>1</web-app>"}),
                          "tomcat-template-v1")
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)

    def test_digest_excludes_build_inputs(self):
        files = render_instance_files(CFG, "orders", TEMPLATES)
        a = config_digest(CFG, files, "tomcat-template-v1")
        b = config_digest(dict(CFG, artifact_pattern="dist/*.war"), files,
                          "tomcat-template-v1")
        self.assertEqual(a, b)
        # 过滤不得改动调用方的字典
        self.assertEqual(CFG["artifact_pattern"], "target/*.war")

    def test_templates_passed_through_verbatim(self):
        files = render_instance_files(CFG, "orders", TEMPLATES)
        self.assertEqual(files["conf/web.xml"], TEMPLATES["web.xml"])
        self.assertEqual(files["conf/catalina.properties"],
                         TEMPLATES["catalina.properties"])
        self.assertEqual(files["conf/logging.properties"],
                         TEMPLATES["logging.properties"])

    def test_xml_attribute_escaping(self):
        # 正常校验会拒绝 &/"；直接构造 cfg 验证渲染层的转义兜底
        cfg = dict(CFG, app_home='/opt/ap&ps"x')
        files = render_instance_files(cfg, "orders", TEMPLATES)
        server = files["conf/server.xml"]
        self.assertIn('appBase="/opt/ap&amp;ps&quot;x/instance/webapps"', server)
        self.assertNotIn("/opt/ap&ps", server)
        ctx = files["conf/Catalina/localhost/orders.xml"]
        self.assertIn('docBase="/opt/ap&amp;ps&quot;x/CURRENT/app.war"', ctx)
        self.assertNotIn("/opt/ap&ps", ctx)

    def test_jvm_opts_cannot_override_platform_flags(self):
        # 用户参数在 java 之后、平台 -D 之前：重复属性按 Java 语义后者生效
        unit = render_war_unit(dict(CFG, jvm_opts=["-Xmx1g"]), "orders")
        exec_start = next(line for line in unit.splitlines()
                          if line.startswith("ExecStart="))
        self.assertLess(exec_start.index("-Xmx1g"),
                        exec_start.index("-Dcatalina.home="))
        self.assertIn("/usr/lib/jvm/java-17/bin/java -Xmx1g "
                      "-Dcatalina.home=/opt/tomcat", exec_start)

    def test_validation_rejects_platform_owned_jvm_opts(self):
        forbidden = (
            "-Dcatalina.base", "-Dcatalina.base=/tmp",
            "-Dcatalina.home", "-Dcatalina.home=/tmp",
            "-Djava.io.tmpdir", "-Djava.io.tmpdir=/tmp",
            "-Djava.util.logging.config.file",
            "-Djava.util.logging.config.file=/tmp/x.properties",
            "-Djava.util.logging.manager",
            "-Djava.util.logging.manager=org.apache.juli.ClassLoaderLogManager",
            "-jar", "-cp", "-classpath", "@/tmp/args",
        )
        for opt in forbidden:
            self.assertTrue(validate_war_config(dict(CFG, jvm_opts=[opt])), opt)

    def test_jvm_opt_with_space_is_one_execstart_word(self):
        unit = render_war_unit(dict(CFG, jvm_opts=["-Dmsg=a b"]), "orders")
        self.assertIn("java '-Dmsg=a b' -Dcatalina.home=/opt/tomcat", unit)

    def test_unit_has_no_raw_specifier_or_env_expansion(self):
        unit = render_war_unit(CFG, "orders")
        exec_start = next(line for line in unit.splitlines()
                          if line.startswith("ExecStart="))
        self.assertIsNone(re.search(r"(?<!%)%(?!%)", exec_start))
        self.assertIsNone(re.search(r"(?<!\$)\$(?!\$)", exec_start))
        # 渲染层加固：上游校验拒绝 %，但渲染仍应把 %/$ 折叠成 %%/$$
        hard = render_war_unit(dict(CFG, jvm_opts=["-Dpct=100%", "-Dhome=$HOME"]),
                               "orders")
        hard_exec = next(line for line in hard.splitlines()
                         if line.startswith("ExecStart="))
        self.assertIn("-Dpct=100%%", hard_exec)
        self.assertIn("'-Dhome=$$HOME'", hard_exec)
        self.assertIsNone(re.search(r"(?<!%)%(?!%)", hard_exec))
        self.assertIsNone(re.search(r"(?<!\$)\$(?!\$)", hard_exec))

    def test_description_cannot_continue_into_next_directive(self):
        unit = render_war_unit(CFG, "orders\\")
        lines = unit.splitlines()
        self.assertIn("Description=orders ", lines)
        self.assertFalse([line for line in lines if line.endswith("\\")])
        self.assertNotIn("orders\\", unit)
        self.assertIn("Description=orders X",
                      render_war_unit(CFG, "orders\rX").splitlines())

    def test_blank_template_raises(self):
        with self.assertRaises(ValueError):
            render_instance_files(CFG, "orders", dict(TEMPLATES, **{"web.xml": "   "}))

    def test_digest_stable_and_includes_unit_content(self):
        files = render_instance_files(CFG, "orders", TEMPLATES)
        unit = render_war_unit(CFG, "orders")
        a = config_digest(CFG, files, "tomcat-template-v1", unit_content=unit)
        self.assertEqual(a, config_digest(CFG, files, "tomcat-template-v1",
                                          unit_content=unit))
        self.assertNotEqual(a, config_digest(CFG, files, "tomcat-template-v2",
                                             unit_content=unit))
        self.assertNotEqual(a, config_digest(CFG, files, "tomcat-template-v1",
                                             unit_content=unit + "\n"))
        # 缺省 unit_content="" 时指纹稳定（unit_sha256 始终存在）
        self.assertEqual(config_digest(CFG, files),
                         config_digest(CFG, files, "tomcat-template-v1",
                                       unit_content=""))
