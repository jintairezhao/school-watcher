"""Translation-key dictionaries are data only when every use translates them."""
import unittest
from scripts.checks.check_localization_catalog import unregistered_script_copy


class LocalizationKeyMapTests(unittest.TestCase):
    catalog={'读取失败':'Read failed','未知':'Unknown'}
    mapping="const labels={failed:'读取失败', unknown:'未知'}; "

    def test_literal_map_used_only_as_translation_key_is_registered(self):
        source=self.mapping+"el.textContent=uiT(labels[node.state] || '未知');"
        self.assertEqual(unregistered_script_copy(source,self.catalog),[])

    def test_missing_map_value_translation_is_still_reported(self):
        source=self.mapping+"el.textContent=uiT(labels[node.state]);"
        self.assertIn('Missing script UI translation: 读取失败',unregistered_script_copy(source,{'未知':'Unknown'}))

    def test_any_untranslated_read_keeps_the_warning(self):
        source=self.mapping+"a.textContent=uiT(labels[node.state]); b.textContent=labels[node.state];"
        self.assertIn('Unregistered script UI text: 读取失败',unregistered_script_copy(source,self.catalog))

    def test_property_mutation_is_not_treated_as_an_immutable_key_map(self):
        source=self.mapping+"labels.failed='新的失败'; el.textContent=uiT(labels[node.state]);"
        self.assertIn('Unregistered script UI text: 读取失败',unregistered_script_copy(source,self.catalog))

    def test_repeated_bindings_are_not_resolved_by_a_global_name_guess(self):
        source='function a(){'+self.mapping+'return uiT(labels[node.state]);} function b(){'+self.mapping+'return uiT(labels[node.state]);}'
        self.assertTrue(unregistered_script_copy(source,self.catalog))

    def test_composed_translation_keys_are_not_assumed_to_be_catalog_entries(self):
        source=self.mapping+"el.textContent=uiT(labels[node.state]+'!');"
        self.assertIn('Unregistered script UI text: 读取失败',unregistered_script_copy(source,self.catalog))

    def test_translation_code_in_a_string_does_not_cover_direct_rendering(self):
        source=self.mapping+"const example='uiT(labels[node.state])'; el.textContent=labels[node.state];"
        self.assertIn('Unregistered script UI text: 读取失败',unregistered_script_copy(source,self.catalog))


if __name__=='__main__':unittest.main()
